"""Gmail client tests with a fully mocked transport (no network).

Covers the Gmail-specific mappings that are easy to get wrong: labels-as-folders
(system labels excluded), conditions -> criteria (OR-joined, quoted), the
archive -> remove-INBOX action, filter round-tripping (no sequence/stop, archive
recovered), the id-then-metadata message read, and move = modify (same id).
"""

from __future__ import annotations

import json

import httpx
import pytest

from rwz_importer.api.gmail_client import GmailClient
from rwz_importer.api.provider import MailboxProvider


def _client(handler, **kwargs) -> GmailClient:
    return GmailClient(
        "test-token",
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
        **kwargs,
    )


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content or b"{}")


def test_capabilities_reports_gmail_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with _client(handler) as client:
        caps = client.capabilities()

    assert caps.ordered_rules is False
    assert caps.stop_processing is False
    assert caps.destination_kind == "label"
    assert caps.archive_toggle is True
    assert caps.hierarchical_folders is False


def test_list_all_folders_returns_user_labels_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/labels")
        return httpx.Response(
            200,
            json={
                "labels": [
                    {"id": "INBOX", "name": "INBOX", "type": "system"},
                    {"id": "L1", "name": "Newsletters", "type": "user"},
                    {"id": "L2", "name": "Work/Projects", "type": "user"},
                ]
            },
        )

    with _client(handler) as client:
        folders = client.list_all_folders()

    assert {(f.id, f.display_name) for f in folders} == {
        ("L1", "Newsletters"),
        ("L2", "Work/Projects"),
    }
    assert client.list_inbox_folders() == []  # flat namespace; avoids double-listing


def test_create_folder_posts_name_and_returns_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path.endswith("/labels")
        assert _body(request)["name"] == "Report"
        return httpx.Response(200, json={"id": "L9", "name": "Report", "type": "user"})

    with _client(handler) as client:
        folder = client.create_folder("Report")

    assert folder.id == "L9" and folder.display_name == "Report"


def test_create_rule_builds_criteria_and_archive_action() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/labels"):
            return httpx.Response(200, json={"labels": [{"id": "L1", "name": "News", "type": "user"}]})
        if request.url.path.endswith("/settings/filters") and request.method == "POST":
            captured["body"] = _body(request)
            return httpx.Response(200, json={"id": "F1", **captured["body"]})
        return httpx.Response(200, json={})

    with _client(handler) as client:
        rule = client.create_rule(
            display_name="ignored-by-gmail",
            sequence=1,
            folder_id="L1",
            from_addresses=["a@x.com", "b@y.com"],
            subject_contains=["Accepted:"],
            archive=True,
        )

    crit = captured["body"]["criteria"]
    act = captured["body"]["action"]
    # Multi-value conditions go through criteria.query (Gmail search syntax), never the
    # structured from/subject fields (which are plain phrase-matches with no OR support).
    assert "from" not in crit and "subject" not in crit
    q = crit["query"]
    assert "from:a@x.com" in q and "from:b@y.com" in q and " OR " in q  # addresses unquoted
    assert 'subject:"Accepted:"' in q  # phrase quoted
    assert act["addLabelIds"] == ["L1"]
    assert act["removeLabelIds"] == ["INBOX"]  # archive=True

    # Round-trips back into an ExistingRule with Gmail semantics.
    assert set(rule.from_addresses) == {"a@x.com", "b@y.com"}
    assert rule.subject_contains == ["Accepted:"]
    assert rule.move_to_folder_id == "L1"
    assert rule.archive is True
    assert rule.sequence is None
    assert rule.stop_processing is False


def test_create_rule_without_archive_keeps_inbox() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/labels"):
            return httpx.Response(200, json={"labels": [{"id": "L1", "name": "News", "type": "user"}]})
        if request.url.path.endswith("/settings/filters") and request.method == "POST":
            return httpx.Response(200, json={"id": "F1", **_body(request)})
        return httpx.Response(200, json={})

    with _client(handler) as client:
        rule = client.create_rule(
            display_name="x", sequence=1, folder_id="L1", from_addresses=["a@x.com"], archive=False
        )

    assert rule.archive is False


def test_multi_value_conditions_round_trip_through_query() -> None:
    """Two senders + two subjects must survive create -> list as a working query filter."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/labels"):
            return httpx.Response(200, json={"labels": [{"id": "L1", "name": "News", "type": "user"}]})
        if request.url.path.endswith("/settings/filters") and request.method == "POST":
            return httpx.Response(200, json={"id": "F1", **_body(request)})
        return httpx.Response(200, json={})

    with _client(handler) as client:
        rule = client.create_rule(
            display_name="x", sequence=1, folder_id="L1",
            from_addresses=["a@x.com", "b@y.com"],
            subject_contains=["Accepted:", "Tentative:"],
        )

    # Every term is recovered (no silent data loss for the common multi-value case).
    assert set(rule.from_addresses) == {"a@x.com", "b@y.com"}
    assert set(rule.subject_contains) == {"Accepted:", "Tentative:"}


def test_parses_structured_criteria_fields() -> None:
    """A filter created in the Gmail UI uses structured from/subject phrase fields."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/labels"):
            return httpx.Response(200, json={"labels": [{"id": "L1", "name": "News", "type": "user"}]})
        if request.url.path.endswith("/settings/filters"):
            return httpx.Response(200, json={"filter": [{
                "id": "F2",
                "criteria": {"from": "boss@x.com", "subject": "Urgent"},
                "action": {"addLabelIds": ["L1"]},
            }]})
        return httpx.Response(200, json={})

    with _client(handler) as client:
        rules = client.list_rules()

    assert rules[0].from_addresses == ["boss@x.com"]
    assert rules[0].subject_contains == ["Urgent"]
    assert rules[0].archive is False  # no INBOX removal


def test_create_rule_requires_condition_and_destination() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"labels": []})

    with _client(handler) as client:
        with pytest.raises(ValueError):  # no conditions
            client.create_rule(display_name="x", sequence=1, folder_id="L1")
        with pytest.raises(ValueError):  # condition but no label and no archive
            client.create_rule(
                display_name="x", sequence=1, folder_id=None,
                from_addresses=["a@x.com"], archive=False,
            )


def test_list_rules_parses_filters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/labels"):
            return httpx.Response(200, json={"labels": [{"id": "L1", "name": "News", "type": "user"}]})
        if request.url.path.endswith("/settings/filters"):
            return httpx.Response(
                200,
                json={
                    "filter": [
                        {
                            "id": "F1",
                            "criteria": {"query": "from:a@x.com"},  # how we store conditions
                            "action": {"addLabelIds": ["L1"], "removeLabelIds": ["INBOX"]},
                        }
                    ]
                },
            )
        return httpx.Response(200, json={})

    with _client(handler) as client:
        rules = client.list_rules()

    assert len(rules) == 1
    r = rules[0]
    assert r.id == "F1"
    assert r.from_addresses == ["a@x.com"]
    assert r.move_to_folder_id == "L1"
    assert r.archive is True
    assert r.sequence is None and r.stop_processing is False
    assert "News" in r.display_name  # destination label name is resolved into the synthesised name


def test_delete_rule_requires_id() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(204)

    with _client(handler) as client:
        with pytest.raises(ValueError):
            client.delete_rule("")
        client.delete_rule("F1")

    assert any("settings/filters/F1" in c for c in calls)
    assert not any(c.endswith("settings/filters/") for c in calls)  # empty id made no request


def test_list_messages_reads_metadata_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/messages"):
            assert request.url.params["q"] == "in:inbox"
            return httpx.Response(200, json={"messages": [{"id": "m1"}, {"id": "m2"}]})
        if path.endswith("/messages/m1"):
            assert request.url.params["format"] == "metadata"
            return httpx.Response(
                200,
                json={
                    "id": "m1",
                    "payload": {"headers": [
                        {"name": "From", "value": "Bob <bob@x.com>"},
                        {"name": "Subject", "value": "Hi"},
                        {"name": "Date", "value": "2026-01-01"},
                    ]},
                },
            )
        if path.endswith("/messages/m2"):
            return httpx.Response(
                200,
                json={"id": "m2", "payload": {"headers": [{"name": "From", "value": "a@b.com"}]}},
            )
        return httpx.Response(200, json={})

    with _client(handler) as client:
        messages = client.list_messages(top=2)

    assert [m.id for m in messages] == ["m1", "m2"]
    assert messages[0].sender_address == "bob@x.com"
    assert messages[0].sender_name == "Bob"
    assert messages[0].subject == "Hi"
    assert messages[1].sender_address == "a@b.com"


def test_move_message_modifies_and_keeps_id() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = _body(request)
        return httpx.Response(200, json={"id": "m1"})  # modify keeps the id

    with _client(handler) as client:
        new_id = client.move_message("m1", "L1")

    assert new_id == "m1"
    assert seen["path"].endswith("/messages/m1/modify")
    assert seen["body"]["addLabelIds"] == ["L1"]
    assert seen["body"]["removeLabelIds"] == ["INBOX"]


def test_move_message_requires_ids() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    with _client(handler) as client:
        with pytest.raises(ValueError):
            client.move_message("", "L1")
        with pytest.raises(ValueError):
            client.move_message("m1", "")

    assert calls == []


def test_gmailclient_satisfies_provider() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with _client(handler) as client:
        assert isinstance(client, MailboxProvider)
