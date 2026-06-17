"""Service-facade tests, driven by an in-memory fake mailbox (no network).

These cover the agent-facing seam: read operations, suggestion pass-through, and
— most importantly — the *apply-with-confirmation* gate that protects the live
mailbox (dry-run by default, a token that must match, refusal on drift, and
cross-mailbox addresses skipped unless explicitly remapped).
"""

from __future__ import annotations

import pytest

from rwz_importer import service
from rwz_importer.api.owa_client import ExistingRule, Folder, Message
from rwz_importer.api.provider import MailboxProvider
from rwz_importer.models import Action, Condition, Flags, Rule, RuleSet


class FakeMailbox:
    """A minimal in-memory MailboxProvider that records every mutation."""

    def __init__(
        self,
        *,
        rules: list[ExistingRule] | None = None,
        folders: list[Folder] | None = None,
        messages: list[Message] | None = None,
    ) -> None:
        self._rules = list(rules or [])
        self._folders = list(folders or [])
        self._messages = list(messages or [])
        self._n = 0
        self.created_folders: list[str] = []
        self.created_rules: list[dict] = []
        self.deleted_rule_ids: list[str] = []

    # -- reads --
    def list_inbox_folders(self) -> list[Folder]:
        return list(self._folders)

    def list_all_folders(self) -> list[Folder]:
        return list(self._folders)

    def list_rules(self) -> list[ExistingRule]:
        return list(self._rules)

    def list_messages(self, *, folder: str = "Inbox", top: int = 200) -> list[Message]:
        return list(self._messages[:top])

    # -- writes --
    def create_folder(self, display_name: str) -> Folder:
        self._n += 1
        folder = Folder(id=f"folder-{self._n}", display_name=display_name)
        self._folders.append(folder)
        self.created_folders.append(display_name)
        return folder

    def create_rule(
        self,
        *,
        display_name: str,
        sequence: int,
        folder_id: str | None,
        from_addresses: list[str] | None = None,
        subject_contains: list[str] | None = None,
        sender_contains: list[str] | None = None,
        stop_processing: bool = True,
        enabled: bool = True,
    ) -> ExistingRule:
        if sequence < 1:
            raise ValueError("Sequence must be >= 1.")
        self._n += 1
        rule = ExistingRule(
            id=f"rule-{self._n}",
            display_name=display_name,
            sequence=sequence,
            is_enabled=enabled,
            from_addresses=list(from_addresses or []),
            subject_contains=list(subject_contains or []),
            sender_contains=list(sender_contains or []),
            move_to_folder_id=folder_id,
            stop_processing=stop_processing,
        )
        self._rules.append(rule)
        self.created_rules.append({"name": display_name, "sequence": sequence})
        return rule

    def delete_rule(self, rule_id: str) -> None:
        self.deleted_rule_ids.append(rule_id)
        self._rules = [r for r in self._rules if r.id != rule_id]

    # -- lifecycle (lets the fake stand in for OwaClient in a `with` block) --
    def close(self) -> None:
        pass

    def __enter__(self) -> "FakeMailbox":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _rule(name: str, folder: str | None = None, *, frm=None, flagged: bool = False) -> Rule:
    return Rule(
        name=name,
        conditions=Condition(fromAddresses=list(frm or [])),
        actions=Action(moveToFolder=folder, stopProcessingRules=True),
        flags=Flags(addressMayBelongToDifferentMailbox=flagged),
    )


def _ruleset(*rules: Rule) -> RuleSet:
    return RuleSet(rules=list(rules), source_file="test.rwz")


def test_fake_satisfies_provider_protocol() -> None:
    assert isinstance(FakeMailbox(), MailboxProvider)


def test_read_helpers_round_trip() -> None:
    mailbox = FakeMailbox(
        rules=[ExistingRule(id="r1", display_name="CI", sequence=2)],
        folders=[Folder(id="f1", display_name="Archive")],
        messages=[Message(id="m1", subject="Hi", sender_address="a@b.com", sender_name="A")],
    )
    assert service.list_rules(mailbox)[0]["name"] == "CI"
    assert service.list_folders(mailbox)[0]["display_name"] == "Archive"
    read = service.read_messages(mailbox, top=10)
    assert read["count"] == 1
    assert read["messages"][0]["sender_address"] == "a@b.com"


def test_suggest_pass_through() -> None:
    messages = [
        Message(id=f"m{i}", subject="Deal", sender_address="news@shop.com", sender_name="Shop")
        for i in range(5)
    ]
    out = service.suggest(FakeMailbox(messages=messages), min_sender=3)
    assert out["sampled_messages"] == 5
    assert any(
        s["conditions"]["fromAddresses"] == ["news@shop.com"] for s in out["suggestions"]
    )
    # Each suggestion carries a ready-to-apply rule object.
    assert out["suggestions"][0]["rule"]["actions"]["stopProcessingRules"] is True


def test_compute_plan_previews_without_writing() -> None:
    mailbox = FakeMailbox()
    plan = service.compute_plan(mailbox, _ruleset(_rule("News", "Newsletters")))

    assert plan["summary"] == {"create": 1, "replace": 0, "skip": 0, "folders": 1}
    assert plan["confirmation_token"].startswith("v1:")
    # Read-only: nothing was created.
    assert mailbox.created_folders == [] and mailbox.created_rules == []
    # Deterministic: recomputing yields the same token.
    again = service.compute_plan(FakeMailbox(), _ruleset(_rule("News", "Newsletters")))
    assert again["confirmation_token"] == plan["confirmation_token"]


def test_execute_dry_run_is_default() -> None:
    mailbox = FakeMailbox()
    out = service.execute_plan(mailbox, _ruleset(_rule("News", "Newsletters")))

    assert out["applied"] is False and out["status"] == "dry_run"
    assert mailbox.created_rules == [] and mailbox.created_folders == []


def test_execute_refuses_without_matching_token() -> None:
    mailbox = FakeMailbox()
    out = service.execute_plan(
        mailbox,
        _ruleset(_rule("News", "Newsletters")),
        confirm=True,
        confirmation_token="v1:not-the-real-token",
    )
    assert out["applied"] is False and out["status"] == "confirmation_required"
    assert mailbox.created_rules == []


def test_execute_applies_with_confirmation() -> None:
    mailbox = FakeMailbox()
    ruleset = _ruleset(_rule("News", "Newsletters"))

    plan = service.compute_plan(mailbox, ruleset)
    out = service.execute_plan(
        mailbox,
        ruleset,
        confirm=True,
        confirmation_token=plan["confirmation_token"],
    )

    assert out["applied"] is True and out["status"] == "applied"
    assert out["created_folders"] == ["Newsletters"]
    assert [r["name"] for r in out["created_rules"]] == ["News"]
    assert out["validation"]["ok"] is True
    # The mutations really landed in the fake mailbox.
    assert "Newsletters" in mailbox.created_folders
    assert any(r["name"] == "News" for r in mailbox.created_rules)


def test_execute_refuses_stale_token_after_drift() -> None:
    mailbox = FakeMailbox()
    ruleset = _ruleset(_rule("News", "Newsletters"))
    plan = service.compute_plan(mailbox, ruleset)  # folder missing -> folder create in plan

    # Mailbox drifts: the destination folder now exists, so the plan changes.
    mailbox._folders.append(Folder(id="pre", display_name="Newsletters"))

    out = service.execute_plan(
        mailbox, ruleset, confirm=True, confirmation_token=plan["confirmation_token"]
    )
    assert out["applied"] is False and out["status"] == "confirmation_required"
    assert out["confirmation_token"] != plan["confirmation_token"]
    assert mailbox.created_rules == []  # nothing written on a stale apply


def test_replace_deletes_old_then_creates() -> None:
    existing = ExistingRule(
        id="old-1", display_name="Team", sequence=1, from_addresses=["old@team.com"]
    )
    mailbox = FakeMailbox(rules=[existing])
    ruleset = _ruleset(_rule("Team", frm=["new@team.com"]))  # same name, different condition

    plan = service.compute_plan(mailbox, ruleset)
    assert plan["rules"][0]["action"] == "REPLACE"

    out = service.execute_plan(
        mailbox, ruleset, confirm=True, confirmation_token=plan["confirmation_token"]
    )
    assert out["applied"] is True
    assert "old-1" in mailbox.deleted_rule_ids
    assert out["replaced_rules"] == ["Team"]


def test_cross_mailbox_flagged_is_skipped_unless_remapped() -> None:
    flagged = _rule("LabOps", frm=["alice.sample@example.org"], flagged=True)
    mailbox = FakeMailbox()

    plan = service.compute_plan(mailbox, _ruleset(flagged))
    assert "LabOps" in plan["skipped_for_review"]
    assert plan["rules"][0]["action"] == "SKIP"

    # Applying does not write the flagged rule.
    out = service.execute_plan(
        mailbox, _ruleset(_rule("LabOps", frm=["alice.sample@example.org"], flagged=True)),
        confirm=True, confirmation_token=plan["confirmation_token"],
    )
    assert out["applied"] is True
    assert mailbox.created_rules == []

    # With an explicit remap the flag is resolved and the rule is created.
    fresh = FakeMailbox()
    remapped_rule = _rule("LabOps", frm=["alice.sample@example.org"], flagged=True)
    plan2 = service.compute_plan(
        fresh, _ruleset(remapped_rule),
        address_map={"alice.sample@example.org": "bob.sample@example.org"},
    )
    assert plan2["rules"][0]["action"] == "CREATE"
    assert plan2["skipped_for_review"] == []
    out2 = service.execute_plan(
        fresh,
        _ruleset(_rule("LabOps", frm=["alice.sample@example.org"], flagged=True)),
        address_map={"alice.sample@example.org": "bob.sample@example.org"},
        confirm=True,
        confirmation_token=plan2["confirmation_token"],
    )
    assert out2["applied"] is True
    assert any(r["name"] == "LabOps" for r in fresh.created_rules)


def test_load_ruleset_requires_exactly_one_source() -> None:
    with pytest.raises(ValueError):
        service.load_ruleset()
    with pytest.raises(ValueError):
        service.load_ruleset(rules_json="{}", rules_json_path="x.json")

    ruleset = service.load_ruleset(
        rules_json='{"rules": [{"name": "X", "actions": {"moveToFolder": "F"}}]}'
    )
    assert ruleset.rules[0].name == "X"
