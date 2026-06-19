"""Google OAuth helper tests — the pure pieces (PKCE, auth URL, token exchange).

The live loopback flow (browser + local server) is not exercised here; these cover the
deterministic, mockable parts that the web callback and CLI flows both rely on.
"""

from __future__ import annotations

import base64
import hashlib
import urllib.parse

import httpx
import pytest

from rwz_importer import auth_google as ag


def test_make_pkce_is_valid_s256() -> None:
    verifier, challenge = ag.make_pkce()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert challenge == expected
    assert "=" not in verifier and "=" not in challenge  # base64url, unpadded


def test_build_auth_url_has_required_params() -> None:
    url = ag.build_auth_url(
        client_id="cid", redirect_uri="http://127.0.0.1:8765/api/auth/google/callback",
        challenge="chal", state="st",
    )
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert url.startswith(ag.AUTH_ENDPOINT)
    assert q["client_id"] == "cid"
    assert q["redirect_uri"] == "http://127.0.0.1:8765/api/auth/google/callback"
    assert q["response_type"] == "code"
    assert q["code_challenge"] == "chal" and q["code_challenge_method"] == "S256"
    assert q["state"] == "st"
    assert q["access_type"] == "offline" and q["prompt"] == "consent"
    assert "gmail.settings.basic" in q["scope"]


def test_exchange_code_posts_grant_and_returns_tokens() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["data"] = dict(urllib.parse.parse_qsl(request.content.decode()))
        return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})

    out = ag.exchange_code(
        client_id="cid", client_secret="sec", code="CODE", verifier="VER",
        redirect_uri="http://127.0.0.1:9/", transport=httpx.MockTransport(handler),
    )
    assert out["access_token"] == "AT" and out["refresh_token"] == "RT"
    assert seen["url"] == ag.TOKEN_ENDPOINT
    assert seen["data"]["grant_type"] == "authorization_code"
    assert seen["data"]["code"] == "CODE" and seen["data"]["code_verifier"] == "VER"


def test_refresh_access_token_posts_refresh_grant() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        data = dict(urllib.parse.parse_qsl(request.content.decode()))
        assert data["grant_type"] == "refresh_token" and data["refresh_token"] == "RT"
        return httpx.Response(200, json={"access_token": "AT2", "expires_in": 3600})

    out = ag.refresh_access_token(
        client_id="cid", client_secret="sec", refresh_token="RT",
        transport=httpx.MockTransport(handler),
    )
    assert out["access_token"] == "AT2"


def test_token_request_raises_on_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "bad code"})

    with pytest.raises(ag.GoogleAuthError) as exc:
        ag.exchange_code(
            client_id="cid", client_secret="sec", code="x", verifier="y",
            redirect_uri="http://127.0.0.1:9/", transport=httpx.MockTransport(handler),
        )
    assert "bad code" in str(exc.value)


def test_credentials_requires_both(monkeypatch) -> None:
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    with pytest.raises(ag.GoogleAuthError):
        ag.credentials()
    assert ag.credentials("cid", "sec") == ("cid", "sec")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "envid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "envsec")
    assert ag.credentials() == ("envid", "envsec")
