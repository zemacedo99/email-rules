"""MCP-layer tests: the tool wrappers correctly drive the service.

The service itself is exercised in ``test_service.py``; here we check the thin
MCP adapter — argument translation (set/dict, inline JSON), the OWA_TOKEN-from-env
guard, and that the apply-with-confirmation token threads through ``plan_apply``
-> ``apply_plan`` unchanged. ``mcp`` is an optional dependency, so the whole
module is skipped when it isn't installed.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

from rwz_importer import mcp_server  # noqa: E402
from tests.test_service import FakeMailbox  # noqa: E402

_RULES_JSON = json.dumps(
    {
        "source_file": "x.rwz",
        "rules": [
            {
                "name": "News",
                "conditions": {"fromAddresses": ["news@shop.com"]},
                "actions": {"moveToFolder": "Newsletters", "stopProcessingRules": True},
            }
        ],
    }
)


@pytest.fixture
def fake_client(monkeypatch) -> FakeMailbox:
    """Point the MCP server's client factory at a shared in-memory mailbox."""
    fake = FakeMailbox()
    monkeypatch.setattr(mcp_server, "_make_client", lambda: fake)
    return fake


def test_live_tool_requires_token(monkeypatch) -> None:
    monkeypatch.delenv("OWA_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="OWA_TOKEN"):
        mcp_server.list_rules()


def test_parse_tool_is_offline(rwz_path) -> None:
    out = mcp_server.parse_rwz_rules(str(rwz_path))
    assert out["rule_count"] == 13
    assert "Email LabOps" in out["flagged_cross_mailbox"]


def test_plan_then_apply_with_confirmation(fake_client: FakeMailbox) -> None:
    plan = mcp_server.plan_apply(rules_json=_RULES_JSON)
    assert plan["summary"] == {"create": 1, "replace": 0, "skip": 0, "folders": 1}
    token = plan["confirmation_token"]
    assert fake_client.created_rules == []  # planning wrote nothing

    # Without confirm it stays a dry run.
    dry = mcp_server.apply_plan(rules_json=_RULES_JSON)
    assert dry["applied"] is False and dry["status"] == "dry_run"
    assert fake_client.created_rules == []

    # With confirm + the real token it applies.
    applied = mcp_server.apply_plan(
        rules_json=_RULES_JSON, confirm=True, confirmation_token=token
    )
    assert applied["applied"] is True
    assert applied["created_folders"] == ["Newsletters"]
    assert [r["name"] for r in applied["created_rules"]] == ["News"]


def test_apply_refuses_bad_token(fake_client: FakeMailbox) -> None:
    out = mcp_server.apply_plan(
        rules_json=_RULES_JSON, confirm=True, confirmation_token="v1:bogus"
    )
    assert out["applied"] is False and out["status"] == "confirmation_required"
    assert fake_client.created_rules == []


def test_exclude_and_map_arguments_translate(fake_client: FakeMailbox) -> None:
    # exclude_rules (list) -> the rule is skipped, nothing planned to create.
    plan = mcp_server.plan_apply(rules_json=_RULES_JSON, exclude_rules=["News"])
    assert plan["summary"]["create"] == 0
    assert plan["rules"][0]["action"] == "SKIP"
