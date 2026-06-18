"""OWA client tests with a fully mocked transport (no network).

Covers the behaviours that are easy to get wrong against this endpoint:
PascalCase parsing, the local ``Sequence >= 1`` guard, ``@odata.nextLink``
pagination, and 429 retry honouring ``Retry-After``.
"""

from __future__ import annotations

import httpx
import pytest

from rwz_importer.api.owa_client import OwaClient
from rwz_importer.api.provider import MailboxProvider


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


def test_list_child_folders_parses_child_count() -> None:
    """Child listing carries ``ChildFolderCount`` so recursion knows where to dig."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "MailFolders/parent-1/childfolders" in str(request.url)
        return httpx.Response(
            200,
            json={"value": [{"Id": "c1", "DisplayName": "Nested", "ChildFolderCount": 2}]},
        )

    with _client(handler) as client:
        children = client.list_child_folders("parent-1")
        assert client.list_child_folders("") == []  # empty id makes no request

    assert len(children) == 1
    assert children[0].id == "c1"
    assert children[0].display_name == "Nested"
    assert children[0].child_folder_count == 2


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


def test_list_messages_parses_sender() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "messages" in str(request.url)
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "Id": "1",
                        "Subject": "Hi",
                        "Sender": {"EmailAddress": {"Name": "Bob", "Address": "bob@x.com"}},
                        "ReceivedDateTime": "2026-01-01T00:00:00Z",
                    }
                ]
            },
        )

    with _client(handler) as client:
        messages = client.list_messages(top=10)

    assert len(messages) == 1
    assert messages[0].sender_address == "bob@x.com"
    assert messages[0].sender_name == "Bob"
    assert messages[0].subject == "Hi"


def test_list_messages_capped() -> None:
    """Every page returns one item and a nextLink; the cap must stop the walk."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    {"Id": "x", "Subject": "s", "Sender": {"EmailAddress": {"Address": "a@b.com"}}}
                ],
                "@odata.nextLink": (
                    "https://outlook.office.com/api/v2.0/me/MailFolders/Inbox/messages?$skip=1"
                ),
            },
        )

    with _client(handler) as client:
        messages = client.list_messages(top=3)

    assert len(messages) == 3


def test_move_message_posts_destination_and_returns_new_id() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(201, json={"Id": "new-id-after-move"})

    with _client(handler) as client:
        new_id = client.move_message("msg-1", "folder-9")

    assert new_id == "new-id-after-move"
    assert seen["method"] == "POST"
    assert "messages/msg-1/move" in seen["url"]
    assert b"folder-9" in seen["body"] and b"DestinationId" in seen["body"]


def test_move_message_requires_ids() -> None:
    """Empty message or folder ids are rejected before any HTTP request."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    with _client(handler) as client:
        with pytest.raises(ValueError):
            client.move_message("", "folder-9")
        with pytest.raises(ValueError):
            client.move_message("msg-1", "")

    assert calls == []


def test_owaclient_satisfies_provider() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": []})

    with _client(handler) as client:
        assert isinstance(client, MailboxProvider)
