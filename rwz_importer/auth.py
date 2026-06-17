"""OAuth 2.0 device-code sign-in — obtain an OWA bearer token without DevTools.

The device-code flow lets the user sign in normally in their browser (so MFA and
Conditional Access still apply) while this tool — which has no client secret —
polls Microsoft for the resulting token. The token is then held in memory by the
caller exactly like a hand-pasted one; it is never written to disk.

Defaults target the first-party **"Microsoft Office"** public client, which is
pre-consented in most tenants for the Outlook REST resource, so no app
registration is required. Every value is overridable (arguments or the
``OWA_OAUTH_*`` environment variables) so a locked-down tenant can point at its
own registration. If a tenant blocks device-code flow entirely, fall back to
pasting a token from the browser Network tab (see ``rwz-importer token-help``).

This module depends only on ``httpx`` (already required) — no MSAL.
"""

from __future__ import annotations

import os
import time
from typing import Callable

import httpx

# First-party "Microsoft Office" client id: public, no secret, and pre-consented
# for the Outlook REST resource in most tenants. Overridable for locked-down orgs.
DEFAULT_CLIENT_ID = "d3590ed6-52b3-4102-aeff-aad2292ab01c"
DEFAULT_TENANT = "common"
DEFAULT_AUTHORITY = "https://login.microsoftonline.com"
# ".default" requests whatever the client is already consented to on the resource,
# yielding a token whose audience is https://outlook.office.com (what OwaClient hits).
DEFAULT_SCOPE = "https://outlook.office.com/.default"

_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class AuthError(RuntimeError):
    """Device-code sign-in could not be completed (declined, expired, or blocked)."""


def _settings(
    client_id: str | None = None,
    tenant: str | None = None,
    scope: str | None = None,
    authority: str | None = None,
) -> tuple[str, str, str, str]:
    """Resolve effective settings: explicit arg → ``OWA_OAUTH_*`` env → built-in default."""
    return (
        client_id or os.environ.get("OWA_OAUTH_CLIENT_ID") or DEFAULT_CLIENT_ID,
        tenant or os.environ.get("OWA_OAUTH_TENANT") or DEFAULT_TENANT,
        scope or os.environ.get("OWA_OAUTH_SCOPE") or DEFAULT_SCOPE,
        (authority or os.environ.get("OWA_OAUTH_AUTHORITY") or DEFAULT_AUTHORITY).rstrip("/"),
    )


def start_device_code(
    *,
    client_id: str | None = None,
    tenant: str | None = None,
    scope: str | None = None,
    authority: str | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> dict:
    """Begin device-code sign-in; return the user code, URL, and poll parameters.

    The returned ``device_code`` is a secret used only to poll for the token — show
    the user ``user_code`` and ``verification_uri`` (or ``verification_uri_complete``),
    never ``device_code``. The chosen client/tenant/authority are echoed back under
    ``_client_id`` / ``_tenant`` / ``_authority`` so polling reuses the same values.
    """
    cid, ten, scp, auth = _settings(client_id, tenant, scope, authority)
    url = f"{auth}/{ten}/oauth2/v2.0/devicecode"
    with httpx.Client(transport=transport, timeout=timeout) as client:
        res = client.post(url, data={"client_id": cid, "scope": scp})
    data = _json(res)
    if res.status_code >= 400 or not data.get("device_code"):
        raise AuthError(
            _describe(data) or f"Device-code request failed (HTTP {res.status_code})."
        )
    data["_client_id"], data["_tenant"], data["_authority"] = cid, ten, auth
    return data


def poll_token_once(
    *,
    device_code: str,
    client_id: str | None = None,
    tenant: str | None = None,
    authority: str | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> dict:
    """Poll the token endpoint once. Never raises for the normal "still waiting" cases.

    Returns one of::

        {"status": "complete", "access_token": ..., "expires_in": int}
        {"status": "pending"}     # user hasn't finished yet — poll again
        {"status": "slow_down"}   # poll again, but wait a little longer
        {"status": "error", "error": ..., "error_description": ...}
    """
    cid, ten, _scope, auth = _settings(client_id, tenant, None, authority)
    url = f"{auth}/{ten}/oauth2/v2.0/token"
    with httpx.Client(transport=transport, timeout=timeout) as client:
        res = client.post(
            url,
            data={"grant_type": _GRANT, "client_id": cid, "device_code": device_code},
        )
    data = _json(res)
    if res.status_code < 400 and data.get("access_token"):
        return {
            "status": "complete",
            "access_token": data["access_token"],
            "expires_in": int(data.get("expires_in", 3600)),
        }
    err = (data.get("error") or "").strip()
    if err == "authorization_pending":
        return {"status": "pending"}
    if err == "slow_down":
        return {"status": "slow_down"}
    return {
        "status": "error",
        "error": err or f"http_{res.status_code}",
        "error_description": _describe(data),
    }


def acquire_token(
    *,
    on_prompt: Callable[[dict], None],
    client_id: str | None = None,
    tenant: str | None = None,
    scope: str | None = None,
    authority: str | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """Run the whole device-code loop and return the access token.

    ``on_prompt(start)`` is called exactly once with the start payload so a CLI or
    UI can display the code and URL. Blocks (sleeping ``interval`` seconds between
    polls) until the user signs in, the code expires, or sign-in is declined.
    """
    start = start_device_code(
        client_id=client_id, tenant=tenant, scope=scope,
        authority=authority, transport=transport, timeout=timeout,
    )
    on_prompt(start)
    interval = max(1, int(start.get("interval", 5)))
    deadline = monotonic() + int(start.get("expires_in", 900))
    while True:
        if monotonic() >= deadline:
            raise AuthError("Sign-in timed out before it was completed.")
        sleep(interval)
        out = poll_token_once(
            device_code=start["device_code"],
            client_id=start["_client_id"],
            tenant=start["_tenant"],
            authority=start["_authority"],
            transport=transport,
            timeout=timeout,
        )
        status = out["status"]
        if status == "complete":
            return out["access_token"]
        if status == "slow_down":
            interval += 5
        elif status != "pending":
            raise AuthError(
                out.get("error_description") or out.get("error") or "Sign-in failed."
            )


# --------------------------------------------------------------------- helpers --
def _json(res: httpx.Response) -> dict:
    try:
        data = res.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _describe(data: dict) -> str:
    """A short, human-readable message from an AAD error body (first line only)."""
    desc = (data.get("error_description") or "").strip()
    if desc:
        return desc.splitlines()[0]
    return (data.get("error") or "").strip()
