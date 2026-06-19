"""MCP server exposing the rwz-importer rule manager to an agent.

Run it as an MCP server (stdio transport) so Claude Code — or any MCP client —
can parse ``.rwz`` exports, inspect a live mailbox, suggest rules from mail
patterns, and apply changes under a strict confirmation gate::

    rwz-importer-mcp          # console entry point
    python -m rwz_importer.mcp_server

Security & safety model
-----------------------
* **The OWA token is read from the ``OWA_TOKEN`` environment variable of this
  server process only.** It is never a tool argument, so it never flows through
  the agent's context or a chat transcript. Set it in the MCP client's server
  config (``env``), exactly as you would a secret. ``OWA_BASE_URL`` optionally
  overrides the endpoint.
* **Exactly one tool writes to the mailbox: ``apply_plan``** (annotated
  destructive). It refuses to write unless the caller passes ``confirm=true`` and
  a ``confirmation_token`` obtained from ``plan_apply`` that still matches the
  freshly recomputed plan — so a human always sees the dry-run plan first, and a
  drifted mailbox forces a re-plan. Everything else is read-only.
* Existing rules are deleted only inside a confirmed ``REPLACE``; folders are
  never deleted; cross-mailbox-flagged addresses are skipped unless explicitly
  remapped via ``map_addresses``.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from rwz_importer import service
from rwz_importer.api import registry
from rwz_importer.api.errors import MailboxApiError
from rwz_importer.api.provider import MailboxProvider

_INSTRUCTIONS = """\
Tools for managing Outlook Web (OWA) inbox rules.

Typical workflow:
1. parse_rwz_rules(file_path)  - turn a .rwz export into editable rules JSON (offline).
2. list_rules / list_folders / read_messages / suggest_rules - inspect the mailbox (read-only).
3. plan_apply(...)             - preview exactly what would change; returns a confirmation_token.
4. Show the plan to the human and get approval.
5. apply_plan(..., confirm=true, confirmation_token=<from step 3>) - the ONLY tool that writes.

Never invent a confirmation_token; it must come from plan_apply. If apply_plan reports the
token is stale, the mailbox changed - re-run plan_apply and re-confirm with the human.
"""

mcp = FastMCP("rwz-importer", instructions=_INSTRUCTIONS)

_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
_OFFLINE = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)


# --------------------------------------------------------------- client setup --
def _make_client() -> MailboxProvider:
    """Build the configured provider from env.

    Token providers: MAIL_PROVIDER (default outlook) + MAIL_TOKEN (or OWA_TOKEN). IMAP:
    MAIL_PROVIDER=imap + MAIL_IMAP_HOST / MAIL_IMAP_USERNAME / MAIL_IMAP_PASSWORD (and
    optional MAIL_IMAP_PORT / MAIL_IMAP_SSL / MAIL_SIEVE_HOST / MAIL_SIEVE_PORT).
    """
    provider = (
        os.environ.get("MAIL_PROVIDER") or os.environ.get("OWA_PROVIDER") or registry.DEFAULT_PROVIDER
    )
    if registry.canonical_provider(provider) == "imap":
        host = os.environ.get("MAIL_IMAP_HOST")
        if not host:
            raise RuntimeError(
                "MAIL_IMAP_HOST is not set. For the IMAP provider set MAIL_IMAP_HOST / "
                "MAIL_IMAP_USERNAME / MAIL_IMAP_PASSWORD (and optional MAIL_IMAP_PORT, "
                "MAIL_IMAP_SSL, MAIL_SIEVE_HOST, MAIL_SIEVE_PORT) in this server's environment."
            )
        return registry.build_provider(provider, {
            "host": host,
            "port": int(os.environ.get("MAIL_IMAP_PORT") or 993),
            "username": os.environ.get("MAIL_IMAP_USERNAME", ""),
            "password": os.environ.get("MAIL_IMAP_PASSWORD", ""),
            "use_ssl": os.environ.get("MAIL_IMAP_SSL", "1") not in ("0", "false", "False"),
            "sieve_host": os.environ.get("MAIL_SIEVE_HOST", ""),
            "sieve_port": int(os.environ.get("MAIL_SIEVE_PORT") or 4190),
        })
    token = os.environ.get("MAIL_TOKEN") or os.environ.get("OWA_TOKEN")
    if not token:
        raise RuntimeError(
            "No mailbox token is set. Set MAIL_TOKEN (or OWA_TOKEN) in this MCP "
            "server's environment (the `env` block of the server config) — never "
            "paste a token into chat. See the token_help tool for how to obtain one. "
            "Set MAIL_PROVIDER=gmail to target Gmail, or MAIL_PROVIDER=imap for IMAP."
        )
    base_url = os.environ.get("MAIL_BASE_URL") or os.environ.get("OWA_BASE_URL")
    return registry.build_provider(provider, {"token": token, "base_url": base_url})


@contextmanager
def _client() -> Iterator[MailboxProvider]:
    """Yield a live provider, mapping API errors to actionable messages."""
    client = _make_client()
    try:
        yield client
    except MailboxApiError as exc:
        raise RuntimeError(_format_api_error(exc)) from exc
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _format_api_error(exc: MailboxApiError) -> str:
    msg = str(exc)
    if exc.status in (401, 403):
        msg += (
            " — the token was rejected. It may be expired (mailbox tokens are "
            "short-lived) or missing the required scope. Use the token_help tool for "
            "how to get a fresh one."
        )
    return msg


# -------------------------------------------------------------- offline tools --
@mcp.tool(annotations=_OFFLINE)
def parse_rwz_rules(file_path: str) -> dict:
    """Parse an Outlook Classic ``.rwz`` export into canonical rules JSON.

    Offline and read-only: reads the binary file at ``file_path`` and returns the
    rules (name, sequence, conditions, destination folder, flags) plus a count and
    the names of any rules flagged as possibly belonging to a different mailbox.
    The returned ``rules`` array can be edited and passed back to ``plan_apply`` /
    ``apply_plan`` as ``rules_json``.
    """
    return service.parse_rules(file_path)


@mcp.tool(annotations=_OFFLINE)
def token_help() -> str:
    """Explain how to obtain an OWA bearer token and where to put it (env, not chat)."""
    return _TOKEN_HELP


# ------------------------------------------------------------ read-only tools --
@mcp.tool(annotations=_READ_ONLY)
def list_rules() -> list[dict]:
    """List the inbox message rules currently on the live mailbox, ordered by sequence."""
    with _client() as client:
        return service.list_rules(client)


@mcp.tool(annotations=_READ_ONLY)
def list_folders() -> list[dict]:
    """List the mailbox's mail folders (Inbox child folders + top-level), with their ids."""
    with _client() as client:
        return service.list_folders(client)


@mcp.tool(annotations=_READ_ONLY)
def read_messages(folder: str = "Inbox", top: int = 200) -> dict:
    """Read recent message *metadata* (sender, subject, date) from ``folder``.

    Message bodies are never fetched. ``top`` caps how many recent messages are
    read (most recent first). Use this to understand mail patterns before
    suggesting rules.
    """
    with _client() as client:
        return service.read_messages(client, folder=folder, top=top)


@mcp.tool(annotations=_READ_ONLY)
def suggest_rules(
    folder: str = "Inbox",
    top: int = 300,
    min_sender: int = 3,
    min_domain: int = 6,
    min_subject: int = 4,
) -> dict:
    """Propose new rules for unfiled mail patterns. Read-only and advisory.

    Samples recent message metadata in ``folder`` and mines it for high-volume
    senders, domain clusters, and recurring bracketed subject tags that no current
    rule already covers. Thresholds (``min_sender`` / ``min_domain`` /
    ``min_subject``) are the minimum message counts to surface each kind. Each
    suggestion includes a ready-to-apply ``rule`` object — feed accepted ones into
    ``plan_apply`` via ``rules_json``.
    """
    with _client() as client:
        return service.suggest(
            client,
            folder=folder,
            top=top,
            min_sender=min_sender,
            min_domain=min_domain,
            min_subject=min_subject,
        )


@mcp.tool(annotations=_READ_ONLY)
def report(format: str = "markdown", mailbox: str | None = None) -> str:
    """Render the live mailbox state (rules + folders) as ``markdown`` or ``json`` text."""
    with _client() as client:
        return service.report(client, fmt=format, mailbox=mailbox)


@mcp.tool(annotations=_READ_ONLY)
def plan_apply(
    file_path: str | None = None,
    rules_json: str | None = None,
    rules_json_path: str | None = None,
    exclude_rules: list[str] | None = None,
    map_addresses: dict[str, str] | None = None,
    normalize_folders: bool = True,
    mailbox: str | None = None,
) -> dict:
    """Preview what applying a rule set would change. Read-only — writes nothing.

    Provide the rules from exactly one source: ``file_path`` (a ``.rwz`` export),
    ``rules_json`` (inline canonical rules JSON, e.g. from ``parse_rwz_rules``), or
    ``rules_json_path``. ``exclude_rules`` drops rules by name; ``map_addresses``
    rewrites from-addresses (``{"old@x": "new@y"}``) and is how you resolve a
    cross-mailbox flag — unresolved flagged rules are reported under
    ``skipped_for_review`` and will be skipped at apply.

    Returns the ordered plan (folders to create, CREATE/REPLACE/SKIP rules with
    sequences) and a ``confirmation_token``. Show this to the human; to actually
    apply, call ``apply_plan`` with ``confirm=true`` and this exact token.
    """
    ruleset = service.load_ruleset(
        file_path=file_path, rules_json_path=rules_json_path, rules_json=rules_json
    )
    with _client() as client:
        return service.compute_plan(
            client,
            ruleset,
            excludes=set(exclude_rules or []),
            address_map=map_addresses or {},
            normalize_folders=normalize_folders,
            mailbox=mailbox,
        )


# ------------------------------------------------------------- the write tool --
@mcp.tool(annotations=_WRITE)
def apply_plan(
    file_path: str | None = None,
    rules_json: str | None = None,
    rules_json_path: str | None = None,
    exclude_rules: list[str] | None = None,
    map_addresses: dict[str, str] | None = None,
    normalize_folders: bool = True,
    confirm: bool = False,
    confirmation_token: str | None = None,
    mailbox: str | None = None,
) -> dict:
    """Apply a rule set to the mailbox. The ONLY tool that writes. Use with care.

    Safety gate (apply-with-confirmation):
    * With ``confirm`` false (the default) this behaves exactly like ``plan_apply``:
      it returns the dry-run plan and a ``confirmation_token`` and writes nothing.
    * To write, you must pass ``confirm=true`` AND ``confirmation_token`` set to the
      value from a prior ``plan_apply``/dry-run for the *same* inputs. If the
      mailbox changed since then, the token no longer matches and the write is
      refused (status ``confirmation_required``) with a fresh token to re-confirm.

    Always show the human the dry-run plan and get their approval before calling
    this with ``confirm=true``. Folders are created as needed; rules are created;
    a rule is deleted only as part of a confirmed REPLACE; cross-mailbox-flagged
    addresses are skipped unless remapped via ``map_addresses``.

    Inputs mirror ``plan_apply``. On success returns the created/replaced folders
    and rules plus a post-apply ``validation`` of the live mailbox.
    """
    ruleset = service.load_ruleset(
        file_path=file_path, rules_json_path=rules_json_path, rules_json=rules_json
    )
    with _client() as client:
        return service.execute_plan(
            client,
            ruleset,
            excludes=set(exclude_rules or []),
            address_map=map_addresses or {},
            normalize_folders=normalize_folders,
            confirm=confirm,
            confirmation_token=confirmation_token,
            mailbox=mailbox,
        )


_TOKEN_HELP = """\
Give this MCP server credentials via its environment (never via chat)
====================================================================

Set MAIL_PROVIDER (outlook | gmail | imap) plus the matching credentials in the MCP
client's server config under `env`:
  - outlook / gmail: MAIL_TOKEN = <bearer/OAuth token>  (OWA_TOKEN still works for outlook)
  - imap:            MAIL_IMAP_HOST / MAIL_IMAP_USERNAME / MAIL_IMAP_PASSWORD
For Gmail, get a token from https://developers.google.com/oauthplayground/ (scopes
gmail.settings.basic gmail.labels gmail.modify). The Outlook manual method follows.

Obtain an OWA bearer token
==========================

This server reads the token from the MAIL_TOKEN/OWA_TOKEN environment variable of its
own process — set it in the MCP client's server config under `env`. Do NOT paste the
token into chat; treat it like a password (it grants mailbox access and expires
after about an hour).

Get the token (Network tab):
1. Sign in to https://outlook.office.com in your browser.
2. Open DevTools (F12) -> Network, open any folder so requests fire, filter "v2.0".
3. Click a request to outlook.office.com/api/v2.0/me/... -> Headers.
4. Copy the "authorization" value after "Bearer ".

Example MCP server config (.mcp.json / claude_desktop_config.json):
    {
      "mcpServers": {
        "rwz-importer": {
          "command": "rwz-importer-mcp",
          "env": { "OWA_TOKEN": "<paste-token-here>" }
        }
      }
    }

Restart the MCP client after editing the config so the new env is picked up.
"""


def main() -> None:
    """Console entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
