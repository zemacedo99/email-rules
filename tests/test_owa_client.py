"""OWA client tests with a fully mocked transport (no network).

Covers the behaviours that are easy to get wrong against this endpoint:
PascalCase parsing, the local ``Sequence >= 1`` guard, ``@odata.nextLink``
pagination, and 429 retry honouring ``Retry-After``.
"""

from __future__ import annotations

import httpx
import pytest

from rwz_importer.api.owa_client import OwaClient


def _client(handler, **kwargs) -> OwaClient:
    return OwaClient(
        "test-token",
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
        **kwargs,
    )


def test_list_folders_parses_display_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [{"Id": "AAA", "DisplayName": "Confluence"}]})

    with _client(handler) as client:
        folders = client.list_inbox_folders()

    assert len(folders) == 1
    assert folders[0].id == "AAA"
    assert folders[0].display_name == "Confluence"


def test_create_folder_returns_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert b"Report" in request.content
        return httpx.Response(201, json={"Id": "FID", "DisplayName": "Report"})

    with _client(handler) as client:
        folder = client.create_folder("Report")

    assert folder.id == "FID"
    assert folder.display_name == "Report"


def test_create_rule_sequence_required() -> None:
    """Sequence 0 is rejected locally, before any HTTP request leaves the client."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    with _client(handler) as client:
        with pytest.raises(ValueError):
            client.create_rule(display_name="X", sequence=0, folder_id="F")

    assert calls == []


def test_pagination_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "skip" in str(request.url).lower():
            return httpx.Response(200, json={"value": [{"Id": "2", "DisplayName": "B"}]})
        return httpx.Response(
            200,
            json={
                "value": [{"Id": "1", "DisplayName": "A"}],
                "@odata.nextLink": "https://outlook.office.com/api/v2.0/me/MailFolders?$skip=100",
            },
        )

    with _client(handler) as client:
        folders = client.list_all_folders()

    assert [f.id for f in folders] == ["1", "2"]


def test_retry_on_429() -> None:
    state = {"n": 0}
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={"value": []})

    client = OwaClient(
        "test-token",
        transport=httpx.MockTransport(handler),
        sleep=lambda s: slept.append(s),
    )
    with client:
        rules = client.list_rules()

    assert rules == []
    assert state["n"] == 2
    assert slept == [0.0]
