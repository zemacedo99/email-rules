"""Tests for the OAuth 2.0 device-code helper (:mod:`rwz_importer.auth`).

Every Microsoft endpoint is faked with :class:`httpx.MockTransport`, so these run
fully offline. We check the two single-shot calls (start, poll) and the blocking
``acquire_token`` loop, including the pending/slow_down/decline/timeout branches.
"""

from __future__ import annotations

import httpx
import pytest

from rwz_importer import auth


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_start_device_code_parses_and_carries_settings() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/oauth2/v2.0/devicecode")
        body = request.content.decode()
        assert "client_id=" in body and "scope=" in body
        return httpx.Response(200, json={
            "device_code": "DEV-SECRET",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 900,
            "interval": 5,
        })

    start = auth.start_device_code(transport=_transport(handler))
    assert start["user_code"] == "ABCD-EFGH"
    assert start["device_code"] == "DEV-SECRET"
    # settings are echoed back so polling reuses the same client/tenant/authority
    assert start["_client_id"] == auth.DEFAULT_CLIENT_ID
    assert start["_tenant"] == auth.DEFAULT_TENANT
    assert start["_authority"] == auth.DEFAULT_AUTHORITY


def test_start_device_code_raises_on_error_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": "invalid_client",
            "error_description": "the first line\nand a second line",
        })

    with pytest.raises(auth.AuthError) as exc:
        auth.start_device_code(transport=_transport(handler))
    assert "the first line" in str(exc.value)
    assert "second line" not in str(exc.value)  # only the first line is surfaced


def test_poll_token_once_pending_then_complete() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(200, json={"access_token": "TOKEN-XYZ", "expires_in": 3600})

    transport = _transport(handler)
    assert auth.poll_token_once(device_code="DEV", transport=transport) == {"status": "pending"}
    done = auth.poll_token_once(device_code="DEV", transport=transport)
    assert done["status"] == "complete" and done["access_token"] == "TOKEN-XYZ"


def test_poll_token_once_slow_down_and_decline() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "slow_down"})

    def declined(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": "authorization_declined", "error_description": "user said no"
        })

    assert auth.poll_token_once(device_code="D", transport=_transport(slow))["status"] == "slow_down"
    out = auth.poll_token_once(device_code="D", transport=_transport(declined))
    assert out["status"] == "error" and out["error"] == "authorization_declined"


def test_acquire_token_loops_until_complete() -> None:
    state = {"poll": 0}
    prompted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("devicecode"):
            return httpx.Response(200, json={
                "device_code": "DEV", "user_code": "WXYZ-1234",
                "verification_uri": "https://microsoft.com/devicelogin",
                "expires_in": 900, "interval": 1,
            })
        state["poll"] += 1
        if state["poll"] < 3:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(200, json={"access_token": "FINAL", "expires_in": 3600})

    token = auth.acquire_token(
        on_prompt=lambda info: prompted.append(info["user_code"]),
        transport=_transport(handler),
        sleep=lambda _s: None,
    )
    assert token == "FINAL"
    assert prompted == ["WXYZ-1234"]  # the user is prompted exactly once
    assert state["poll"] == 3


def test_acquire_token_raises_on_decline() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("devicecode"):
            return httpx.Response(200, json={
                "device_code": "DEV", "user_code": "X", "interval": 1,
                "expires_in": 900, "verification_uri": "u",
            })
        return httpx.Response(400, json={
            "error": "authorization_declined", "error_description": "declined"
        })

    with pytest.raises(auth.AuthError):
        auth.acquire_token(
            on_prompt=lambda _i: None, transport=_transport(handler), sleep=lambda _s: None
        )


def test_acquire_token_times_out() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("devicecode"):
            return httpx.Response(200, json={
                "device_code": "DEV", "user_code": "X", "interval": 1,
                "expires_in": 900, "verification_uri": "u",
            })
        return httpx.Response(400, json={"error": "authorization_pending"})

    clock = iter([0.0, 10_000.0])  # second reading is well past the deadline
    with pytest.raises(auth.AuthError, match="timed out"):
        auth.acquire_token(
            on_prompt=lambda _i: None, transport=_transport(handler),
            sleep=lambda _s: None, monotonic=lambda: next(clock),
        )
