"""Starlette app exposing the rwz-importer service as a small localhost JSON API.

``create_app(client_factory)`` is the testable seam: the factory returns a
:class:`~rwz_importer.api.provider.MailboxProvider`. In production the default
factory builds the configured provider via :mod:`rwz_importer.api.registry`
(Outlook or Gmail) from the token held in memory; tests inject a fake. Endpoints
map one-to-one onto :mod:`rwz_importer.service`.

Security posture: the bearer token is kept in process memory only (optionally
seeded from ``OWA_TOKEN``), never persisted, never returned to the client, never
logged or put in a URL. :func:`run` refuses any non-loopback bind.
"""

from __future__ import annotations

import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import httpx
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from rwz_importer import auth, service
from rwz_importer.api import registry
from rwz_importer.api.errors import MailboxApiError
from rwz_importer.api.provider import MailboxProvider

_INDEX_HTML = Path(__file__).parent / "index.html"

ProviderFactory = Callable[[], MailboxProvider]


class _NeedsToken(RuntimeError):
    """Raised when a live operation is attempted before a token is configured."""


def _initial_provider() -> str:
    env = os.environ.get("MAIL_PROVIDER") or os.environ.get("OWA_PROVIDER")
    try:
        return registry.canonical_provider(env or registry.DEFAULT_PROVIDER)
    except ValueError:
        return registry.DEFAULT_PROVIDER


class _Config:
    """In-memory server config. The token never leaves this process's memory."""

    def __init__(self) -> None:
        self.provider: str = _initial_provider()
        self.token: str | None = (
            os.environ.get("MAIL_TOKEN") or os.environ.get("OWA_TOKEN") or None
        )
        self.mailbox: str | None = None
        # None means "use the selected provider's default endpoint" (registry fills it in).
        self.base_url: str | None = (
            os.environ.get("MAIL_BASE_URL") or os.environ.get("OWA_BASE_URL") or None
        )
        # Pending device-code sign-in, if one is in flight. Holds the secret
        # device_code + poll settings; never serialised to the browser.
        self.device: dict | None = None

    def public_view(self) -> dict:
        """Config safe to send to the browser — the token value is never included."""
        info = registry.provider_info(self.provider)
        return {
            "token_set": bool(self.token),
            "mailbox": self.mailbox,
            "provider": self.provider,
            "base_url": self.base_url or info["default_base_url"],
            "capabilities": info["capabilities"],
            "providers": registry.all_provider_info(),
        }


def create_app(client_factory: ProviderFactory | None = None) -> Starlette:
    """Build the Starlette app. ``client_factory`` overrides how a provider is made."""
    config = _Config()

    if client_factory is None:
        def client_factory() -> MailboxProvider:  # default: real provider via the registry
            if not config.token:
                raise _NeedsToken("No token set. Connect an account in the Connection panel first.")
            return registry.build_provider(
                config.provider, {"token": config.token, "base_url": config.base_url}
            )

    @contextmanager
    def provider() -> Iterator[MailboxProvider]:
        client = client_factory()
        try:
            yield client
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    async def _run(job: Callable[[], object]) -> Response:
        """Run a blocking, provider-using job off the event loop; map errors to JSON."""
        try:
            result = await run_in_threadpool(job)
        except _NeedsToken as exc:
            return JSONResponse({"error": str(exc), "needs_token": True}, status_code=400)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except MailboxApiError as exc:
            return JSONResponse(
                {"error": str(exc), "status": exc.status}, status_code=502
            )
        return JSONResponse(result)

    # -- static -----------------------------------------------------------------
    async def index(request: Request) -> Response:
        return FileResponse(_INDEX_HTML)

    # -- config (no provider; never echoes the token) ---------------------------
    async def get_config(request: Request) -> Response:
        return JSONResponse(config.public_view())

    async def post_config(request: Request) -> Response:
        body = await _body(request)
        if body.get("provider"):
            try:
                new_provider = registry.canonical_provider(str(body["provider"]))
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            if new_provider != config.provider:
                # A token/endpoint/mailbox is provider-specific — switching invalidates them.
                config.provider = new_provider
                config.token = None
                config.base_url = None
                config.mailbox = None
                config.device = None
        if body.get("clear_token"):
            config.token = None
        elif body.get("token"):
            config.token = str(body["token"]).strip()
        if "mailbox" in body:
            config.mailbox = (body.get("mailbox") or "").strip() or None
        if "base_url" in body:
            config.base_url = str(body.get("base_url") or "").strip() or None
        return JSONResponse(config.public_view())

    # -- sign in with Microsoft (device code; token stays in memory) ------------
    async def post_auth_start(request: Request) -> Response:
        def job() -> object:
            info = auth.start_device_code()
            config.device = {
                "device_code": info["device_code"],
                "client_id": info.get("_client_id"),
                "tenant": info.get("_tenant"),
                "authority": info.get("_authority"),
                "interval": max(1, int(info.get("interval", 5))),
                "deadline": time.monotonic() + int(info.get("expires_in", 900)),
            }
            # Only the user-facing bits cross to the browser — never device_code.
            return {
                "user_code": info.get("user_code"),
                "verification_uri": info.get("verification_uri")
                or info.get("verification_url"),
                "verification_uri_complete": info.get("verification_uri_complete"),
                "message": info.get("message"),
                "interval": config.device["interval"],
                "expires_in": int(info.get("expires_in", 900)),
            }

        try:
            result = await run_in_threadpool(job)
        except auth.AuthError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except httpx.HTTPError as exc:
            return JSONResponse(
                {"error": f"Could not reach Microsoft sign-in: {exc}"}, status_code=502
            )
        return JSONResponse(result)

    async def post_auth_poll(request: Request) -> Response:
        def job() -> object:
            dev = config.device
            if not dev:
                return {"status": "idle"}
            if time.monotonic() >= dev["deadline"]:
                config.device = None
                return {
                    "status": "error",
                    "error": "expired_token",
                    "error_description": "The sign-in code expired. Start again.",
                }
            out = auth.poll_token_once(
                device_code=dev["device_code"],
                client_id=dev["client_id"],
                tenant=dev["tenant"],
                authority=dev["authority"],
            )
            if out["status"] == "complete":
                config.token = out["access_token"]  # in-memory only
                config.device = None
                return {"status": "complete", "token_set": True}
            if out["status"] in ("pending", "slow_down"):
                return {"status": out["status"]}
            config.device = None
            return {
                "status": "error",
                "error": out.get("error"),
                "error_description": out.get("error_description"),
            }

        # Poll always answers 200 with a status the client loop can read; even a
        # network blip becomes a soft error rather than throwing in the browser.
        try:
            result = await run_in_threadpool(job)
        except (auth.AuthError, httpx.HTTPError) as exc:
            return JSONResponse(
                {"status": "error", "error": "network", "error_description": str(exc)}
            )
        return JSONResponse(result)

    async def post_auth_cancel(request: Request) -> Response:
        config.device = None
        return JSONResponse({"status": "cancelled"})

    # -- rules CRUD -------------------------------------------------------------
    async def get_rules(request: Request) -> Response:
        return await _run(lambda: _with(provider, service.list_rules))

    async def post_rules(request: Request) -> Response:
        body = await _body(request)
        conditions = body.get("conditions") or {}

        def job() -> object:
            with provider() as client:
                return service.create_rule(
                    client,
                    name=body.get("name", ""),
                    from_addresses=conditions.get("fromAddresses"),
                    subject_contains=conditions.get("subjectContains"),
                    sender_contains=conditions.get("senderContains"),
                    move_to_folder=body.get("move_to_folder"),
                    stop_processing=bool(body.get("stop_processing", True)),
                    enabled=bool(body.get("enabled", True)),
                    archive=bool(body.get("archive", True)),
                )

        return await _run(job)

    async def rules_item(request: Request) -> Response:
        rule_id = request.path_params["rule_id"]
        if request.method == "DELETE":
            return await _run(
                lambda: _with(provider, lambda c: service.delete_rule(c, rule_id))
            )
        body = await _body(request)
        conditions = body.get("conditions") or {}

        def job() -> object:
            with provider() as client:
                return service.update_rule(
                    client,
                    rule_id,
                    name=body.get("name", ""),
                    from_addresses=conditions.get("fromAddresses"),
                    subject_contains=conditions.get("subjectContains"),
                    sender_contains=conditions.get("senderContains"),
                    move_to_folder=body.get("move_to_folder"),
                    stop_processing=bool(body.get("stop_processing", True)),
                    enabled=bool(body.get("enabled", True)),
                    # Omitted -> inherit the existing rule's archive (never silently flip it).
                    archive=bool(body["archive"]) if "archive" in body else None,
                )

        return await _run(job)

    # -- folders / suggest ------------------------------------------------------
    async def get_folders(request: Request) -> Response:
        return await _run(lambda: _with(provider, service.list_folders))

    async def post_suggest(request: Request) -> Response:
        body = await _body(request)

        def job() -> object:
            with provider() as client:
                return service.suggest(
                    client,
                    folder=body.get("folder", "Inbox"),
                    top=_sample_top(body.get("top")),
                    min_sender=int(body.get("min_sender", 3)),
                    min_domain=int(body.get("min_domain", 6)),
                    min_subject=int(body.get("min_subject", 4)),
                )

        return await _run(job)

    # -- run rules over the backlog (preview, then confirm) ---------------------
    async def post_preview_run(request: Request) -> Response:
        body = await _body(request)
        rule_ids = body.get("rule_ids")

        def job() -> object:
            with provider() as client:
                return service.preview_run(
                    client,
                    rule_ids=list(rule_ids) if rule_ids is not None else None,
                    folder=body.get("folder", "Inbox"),
                    top=int(body.get("top", 300)),
                )

        return await _run(job)

    async def post_execute_run(request: Request) -> Response:
        body = await _body(request)
        moves = body.get("moves") or []

        def job() -> object:
            with provider() as client:
                return service.execute_run(client, list(moves))

        return await _run(job)

    # -- import a .rwz (parse offline, then plan/confirm) -----------------------
    async def post_rwz_parse(request: Request) -> Response:
        form = await request.form()
        upload = form.get("file")
        if upload is None:
            return JSONResponse({"error": "No .rwz file uploaded."}, status_code=400)
        data = await upload.read()
        filename = getattr(upload, "filename", "uploaded.rwz")
        return await _run(lambda: _parse_rwz_bytes(data, filename))

    async def post_rwz_plan(request: Request) -> Response:
        body = await _body(request)

        def job() -> object:
            ruleset = service.load_ruleset(rules_json=body.get("rules_json") or "")
            with provider() as client:
                return service.compute_plan(
                    client,
                    ruleset,
                    excludes=set(body.get("exclude_rules") or []),
                    address_map=body.get("map_addresses") or None,
                    mailbox=body.get("mailbox") or config.mailbox,
                )

        return await _run(job)

    async def post_rwz_apply(request: Request) -> Response:
        body = await _body(request)

        def job() -> object:
            ruleset = service.load_ruleset(rules_json=body.get("rules_json") or "")
            with provider() as client:
                return service.execute_plan(
                    client,
                    ruleset,
                    excludes=set(body.get("exclude_rules") or []),
                    address_map=body.get("map_addresses") or None,
                    confirm=True,
                    confirmation_token=body.get("confirmation_token"),
                    mailbox=body.get("mailbox") or config.mailbox,
                )

        return await _run(job)

    routes = [
        Route("/", index, methods=["GET"]),
        Route("/api/config", get_config, methods=["GET"]),
        Route("/api/config", post_config, methods=["POST"]),
        Route("/api/auth/start", post_auth_start, methods=["POST"]),
        Route("/api/auth/poll", post_auth_poll, methods=["POST"]),
        Route("/api/auth/cancel", post_auth_cancel, methods=["POST"]),
        Route("/api/rules", get_rules, methods=["GET"]),
        Route("/api/rules", post_rules, methods=["POST"]),
        Route("/api/rules/{rule_id}", rules_item, methods=["PUT", "DELETE"]),
        Route("/api/folders", get_folders, methods=["GET"]),
        Route("/api/suggest", post_suggest, methods=["POST"]),
        Route("/api/preview-run", post_preview_run, methods=["POST"]),
        Route("/api/execute-run", post_execute_run, methods=["POST"]),
        Route("/api/rwz/parse", post_rwz_parse, methods=["POST"]),
        Route("/api/rwz/plan", post_rwz_plan, methods=["POST"]),
        Route("/api/rwz/apply", post_rwz_apply, methods=["POST"]),
    ]
    return Starlette(routes=routes)


# --------------------------------------------------------------------- helpers --
def _with(provider_cm, fn):
    """Open a provider and call ``fn(client)`` — keeps simple handlers one-liners."""
    with provider_cm() as client:
        return fn(client)


async def _body(request: Request) -> dict:
    """Parse a JSON request body, tolerating an empty/absent body as ``{}``."""
    try:
        parsed = await request.json()
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# Sampling "all" of a mailbox is just a very high cap: the OWA client paginates
# until ``@odata.nextLink`` is exhausted or the cap is hit, so this drains any
# normal inbox while staying bounded for a pathological one.
_SAMPLE_ALL = 100_000


def _sample_top(value: object, default: int = 300) -> int:
    """Map a request's ``top`` to a positive cap. ``"all"``/``0``/``None`` → all."""
    if value is None:
        return default
    if isinstance(value, str) and value.strip().lower() == "all":
        return _SAMPLE_ALL
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return _SAMPLE_ALL if n <= 0 else n


def _parse_rwz_bytes(data: bytes, filename: str) -> dict:
    """Parse uploaded ``.rwz`` bytes offline. Returns the rules dict + editable JSON."""
    tmp = tempfile.NamedTemporaryFile(suffix=".rwz", delete=False)
    try:
        tmp.write(data)
        tmp.close()
        ruleset = service.load_ruleset(file_path=tmp.name)
    finally:
        os.unlink(tmp.name)
    if filename:
        ruleset.source_file = filename
    parsed = ruleset.to_dict()
    parsed["rule_count"] = len(ruleset.rules)
    parsed["flagged_cross_mailbox"] = [
        r.name for r in ruleset.rules if r.flags.addressMayBelongToDifferentMailbox
    ]
    return {"parsed": parsed, "rules_json": ruleset.to_json()}


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve the UI on loopback. Refuses any non-loopback host (it holds a token)."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit(
            f"Refusing to bind to {host!r}: this server holds your mailbox token and "
            "must stay on loopback. Use 127.0.0.1."
        )
    import uvicorn

    print(f"rwz-importer web UI on http://{host}:{port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


def main() -> None:
    """Console entry point (``rwz-importer-web``)."""
    port = int(os.environ.get("RWZ_WEB_PORT", "8765"))
    run(port=port)


if __name__ == "__main__":
    main()
