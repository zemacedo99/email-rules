"""Google OAuth 2.0 sign-in (Authorization Code + PKCE, loopback redirect) for Gmail.

Unlike Microsoft, Google has no first-party pre-consented client, so the user creates
their own OAuth client (type **Desktop app**) in Google Cloud and supplies its client
id + secret (a desktop-client secret is not confidential, but is still kept in memory
only). This module implements the loopback installed-app flow: the user signs in in
their browser, Google redirects to a ``127.0.0.1`` URL the tool listens on, and the
authorization code is exchanged — with the PKCE verifier — for an access token (and a
refresh token, so the session can outlive the ~1h access-token lifetime).

The pure pieces (PKCE, auth-URL building, code/refresh exchange) are reused by both the
web callback flow (which redirects to the running web server's own port) and the CLI
:func:`run_loopback_flow` (which spins up a throwaway loopback server). Depends only on
``httpx`` (already required) + the stdlib.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
import urllib.parse
from typing import Callable

import httpx

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
# The Gmail scopes this tool needs: manage filters + labels, and read/modify messages.
DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/gmail.settings.basic "
    "https://www.googleapis.com/auth/gmail.labels "
    "https://www.googleapis.com/auth/gmail.modify"
)


class GoogleAuthError(RuntimeError):
    """Google sign-in could not be completed (declined, expired, or misconfigured)."""


def credentials(
    client_id: str | None = None, client_secret: str | None = None
) -> tuple[str, str]:
    """Resolve the OAuth client id/secret: explicit arg → ``GOOGLE_OAUTH_*`` env."""
    cid = client_id or os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or ""
    secret = client_secret or os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or ""
    if not cid or not secret:
        raise GoogleAuthError(
            "Google sign-in needs an OAuth client id and secret. Create a 'Desktop app' "
            "OAuth client in Google Cloud (with the Gmail API enabled) and supply them via "
            "the Connection panel or GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET."
        )
    return cid, secret


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_pkce() -> tuple[str, str]:
    """Return a (verifier, challenge) PKCE pair (S256)."""
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def new_state() -> str:
    """An unguessable state token for CSRF protection on the redirect."""
    return secrets.token_urlsafe(24)


def build_auth_url(
    *,
    client_id: str,
    redirect_uri: str,
    challenge: str,
    state: str,
    scope: str = DEFAULT_SCOPES,
) -> str:
    """Build the Google authorization URL to send the user to."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",  # ask for a refresh token
        "prompt": "consent",       # ensure a refresh token is returned each time
    }
    return AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)


def _token_request(
    data: dict, *, transport: httpx.BaseTransport | None = None, timeout: float = 30.0
) -> dict:
    with httpx.Client(transport=transport, timeout=timeout) as client:
        res = client.post(TOKEN_ENDPOINT, data=data)
    try:
        body = res.json()
    except Exception:
        body = {}
    if res.status_code >= 400 or not body.get("access_token"):
        msg = body.get("error_description") or body.get("error") or f"HTTP {res.status_code}"
        raise GoogleAuthError(f"Google token request failed: {msg}")
    return body


def exchange_code(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    verifier: str,
    redirect_uri: str,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> dict:
    """Exchange an authorization code (+ PKCE verifier) for tokens."""
    return _token_request(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        transport=transport,
        timeout=timeout,
    )


def refresh_access_token(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> dict:
    """Use a refresh token to mint a fresh access token (no browser needed)."""
    return _token_request(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        transport=transport,
        timeout=timeout,
    )


def run_loopback_flow(
    *,
    client_id: str,
    client_secret: str,
    on_prompt: Callable[[str], None],
    scope: str = DEFAULT_SCOPES,
    open_browser: bool = True,
    host: str = "127.0.0.1",
    timeout: float = 300.0,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """Run the whole installed-app flow on a throwaway loopback server (for the CLI).

    Starts an HTTP server on a free ``127.0.0.1`` port, calls ``on_prompt(url)`` with the
    authorization URL (and opens a browser unless disabled), waits for Google's redirect,
    and exchanges the code. Returns the token payload (access_token, refresh_token, …).
    """
    import http.server

    verifier, challenge = make_pkce()
    state = new_state()
    captured: dict[str, str] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib API name
            captured.update(dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query)))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body style='font-family:sans-serif'><h2>Signed in.</h2>"
                b"<p>You can close this tab and return to Email Rule Manager.</p></body></html>"
            )

        def log_message(self, *args) -> None:  # silence the default stderr logging
            return

    server = http.server.HTTPServer((host, 0), _Handler)
    try:
        port = server.server_address[1]
        redirect_uri = f"http://{host}:{port}/"
        url = build_auth_url(
            client_id=client_id, redirect_uri=redirect_uri, challenge=challenge,
            state=state, scope=scope,
        )
        on_prompt(url)
        if open_browser:
            try:
                import webbrowser

                webbrowser.open(url)
            except Exception:  # pragma: no cover - environment dependent
                pass
        server.timeout = 1
        deadline = time.monotonic() + timeout
        while "code" not in captured and "error" not in captured:
            if time.monotonic() > deadline:
                raise GoogleAuthError("Sign-in timed out before it was completed.")
            server.handle_request()
    finally:
        server.server_close()

    if captured.get("error"):
        raise GoogleAuthError(f"Sign-in failed: {captured['error']}")
    if captured.get("state") != state:
        raise GoogleAuthError("State mismatch on the redirect (possible CSRF); aborted.")
    return exchange_code(
        client_id=client_id, client_secret=client_secret, code=captured["code"],
        verifier=verifier, redirect_uri=redirect_uri, transport=transport,
    )
