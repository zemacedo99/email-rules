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
from rwz_importer.api.provider import MailboxProvider, ProviderCapabilities
from rwz_importer.models import Action, Condition, Flags, Rule, RuleSet


class FakeMailbox:
    """A minimal in-memory MailboxProvider that records every mutation."""

    def __init__(
        self,
        *,
        rules: list[ExistingRule] | None = None,
        folders: list[Folder] | None = None,
        messages: list[Message] | None = None,
        child_folders: dict[str, list[Folder]] | None = None,
        capabilities: ProviderCapabilities | None = None,
    ) -> None:
        self._rules = list(rules or [])
        self._folders = list(folders or [])
        self._messages = list(messages or [])
        self._child_folders = {k: list(v) for k, v in (child_folders or {}).items()}
        self._caps = capabilities or ProviderCapabilities()
        self._n = 0
        self.created_folders: list[str] = []
        self.created_rules: list[dict] = []
        self.deleted_rule_ids: list[str] = []
        self.moved_messages: list[tuple[str, str]] = []

    def capabilities(self) -> ProviderCapabilities:
        return self._caps

    # -- reads --
    def list_inbox_folders(self) -> list[Folder]:
        return list(self._folders)

    def list_all_folders(self) -> list[Folder]:
        return list(self._folders)

    def list_child_folders(self, parent_id: str) -> list[Folder]:
        return list(self._child_folders.get(parent_id, []))

    def list_rules(self) -> list[ExistingRule]:
        return list(self._rules)

    def list_messages(self, *, folder: str = "Inbox", top: int = 200) -> list[Message]:
        return list(self._messages[:top])

    def move_message(self, message_id: str, destination_folder_id: str) -> str:
        self.moved_messages.append((message_id, destination_folder_id))
        self._messages = [m for m in self._messages if m.id != message_id]
        self._n += 1
        return f"moved-{self._n}"

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
        archive: bool = True,
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
            archive=archive,
        )
        self._rules.append(rule)
        self.created_rules.append(
            {"name": display_name, "sequence": sequence, "archive": archive}
        )
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


def test_list_folders_recurses_into_nested_subfolders() -> None:
    # Work (a listed folder) -> Projects -> Alpha: two levels below what a single
    # OWA listing returns, so they only surface if _collect_folders recurses.
    work = Folder(id="work", display_name="Work", child_folder_count=1)
    projects = Folder(id="proj", display_name="Projects", child_folder_count=1)
    alpha = Folder(id="alpha", display_name="Alpha")
    mailbox = FakeMailbox(
        folders=[work],
        child_folders={"work": [projects], "proj": [alpha]},
    )
    by_id = {f["id"]: f["display_name"] for f in service.list_folders(mailbox)}
    assert by_id == {"work": "Work", "proj": "Projects", "alpha": "Alpha"}


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


def test_create_rule_creates_folder_and_appends() -> None:
    mailbox = FakeMailbox(rules=[ExistingRule(id="r1", display_name="A", sequence=1)])
    out = service.create_rule(
        mailbox,
        name="News",
        from_addresses=["  news@shop.com  ", ""],
        move_to_folder="Newsletters",
    )

    assert out["created_folder"] == "Newsletters"
    assert out["created"]["name"] == "News"
    # Appended after the existing rule (sequence 1) -> sequence 2.
    assert out["created"]["sequence"] == 2
    assert mailbox.created_folders == ["Newsletters"]
    # Whitespace trimmed, empties dropped.
    assert out["created"]["conditions"]["fromAddresses"] == ["news@shop.com"]


def test_create_rule_reuses_existing_folder() -> None:
    mailbox = FakeMailbox(folders=[Folder(id="f1", display_name="Newsletters")])
    out = service.create_rule(mailbox, name="News", move_to_folder="newsletters")

    assert out["created_folder"] is None  # matched case-insensitively, not recreated
    assert mailbox.created_folders == []
    assert mailbox.created_rules[0]["name"] == "News"


def test_create_rule_requires_name() -> None:
    with pytest.raises(ValueError):
        service.create_rule(FakeMailbox(), name="   ")


def test_update_rule_replaces_reusing_sequence() -> None:
    existing = ExistingRule(
        id="old-1", display_name="Team", sequence=4, from_addresses=["old@team.com"]
    )
    mailbox = FakeMailbox(rules=[existing])

    out = service.update_rule(
        mailbox, "old-1", name="Team", from_addresses=["new@team.com"]
    )

    assert out["replaced_rule_id"] == "old-1"
    assert "old-1" in mailbox.deleted_rule_ids
    assert out["updated"]["sequence"] == 4  # old sequence reused
    assert out["updated"]["conditions"]["fromAddresses"] == ["new@team.com"]


def test_update_rule_unknown_id_raises() -> None:
    with pytest.raises(ValueError):
        service.update_rule(FakeMailbox(), "nope", name="X")


def test_delete_rule_removes_and_reports_name() -> None:
    mailbox = FakeMailbox(rules=[ExistingRule(id="r1", display_name="Spam", sequence=1)])
    out = service.delete_rule(mailbox, "r1")

    assert out == {"deleted_rule_id": "r1", "name": "Spam"}
    assert mailbox.deleted_rule_ids == ["r1"]
    assert mailbox.list_rules() == []


def test_delete_rule_unknown_id_raises() -> None:
    with pytest.raises(ValueError):
        service.delete_rule(FakeMailbox(), "nope")


def test_preview_run_matches_first_rule_only_and_writes_nothing() -> None:
    rules = [
        ExistingRule(
            id="r1", display_name="Shop", sequence=1,
            from_addresses=["news@shop.com"], move_to_folder_id="f-shop",
        ),
        # Lower priority rule that would also match on the domain substring.
        ExistingRule(
            id="r2", display_name="AnyShop", sequence=2,
            sender_contains=["shop.com"], move_to_folder_id="f-any",
        ),
    ]
    messages = [
        Message(id="m1", subject="Deal", sender_address="news@shop.com", sender_name="Shop"),
        Message(id="m2", subject="Other", sender_address="hr@corp.com", sender_name="HR"),
    ]
    mailbox = FakeMailbox(
        rules=rules,
        folders=[Folder(id="f-shop", display_name="Shop"), Folder(id="f-any", display_name="Any")],
        messages=messages,
    )

    out = service.preview_run(mailbox)

    assert out["sampled_messages"] == 2
    assert out["total_matches"] == 1  # m1 assigned to r1 only, not double-counted
    assert out["moves"][0] == {
        "message_id": "m1",
        "folder_id": "f-shop",
        "folder": "Shop",
        "rule_id": "r1",
        "rule_name": "Shop",
        "subject": "Deal",
        "sender_address": "news@shop.com",
    }
    by_rule = {r["rule_id"]: r["match_count"] for r in out["rules"]}
    assert by_rule == {"r1": 1, "r2": 0}
    # Read-only: nothing moved.
    assert mailbox.moved_messages == []


def test_preview_run_skips_rule_without_folder_and_never_matches_empty() -> None:
    rules = [
        ExistingRule(id="r1", display_name="NoDest", sequence=1, subject_contains=["x"]),
        ExistingRule(id="r2", display_name="NoCond", sequence=2, move_to_folder_id="f1"),
    ]
    messages = [Message(id="m1", subject="x marks", sender_address="a@b.com")]
    mailbox = FakeMailbox(
        rules=rules, folders=[Folder(id="f1", display_name="Dest")], messages=messages
    )

    out = service.preview_run(mailbox)

    assert "NoDest" in out["skipped_no_folder"]  # no move target -> not runnable
    assert out["total_matches"] == 0  # NoCond has no conditions -> never matches
    assert out["moves"] == []


def test_preview_run_can_target_specific_rule_ids() -> None:
    rules = [
        ExistingRule(id="r1", display_name="A", sequence=1,
                     subject_contains=["hi"], move_to_folder_id="f1"),
        ExistingRule(id="r2", display_name="B", sequence=2,
                     subject_contains=["hi"], move_to_folder_id="f2"),
    ]
    messages = [Message(id="m1", subject="hi there", sender_address="a@b.com")]
    mailbox = FakeMailbox(
        rules=rules,
        folders=[Folder(id="f1", display_name="F1"), Folder(id="f2", display_name="F2")],
        messages=messages,
    )

    out = service.preview_run(mailbox, rule_ids=["r2"])

    assert [r["rule_id"] for r in out["rules"]] == ["r2"]
    assert out["moves"][0]["folder_id"] == "f2"


def test_execute_run_moves_each_and_collects_failures() -> None:
    messages = [
        Message(id="m1", subject="a", sender_address="a@b.com"),
        Message(id="m2", subject="b", sender_address="b@b.com"),
    ]
    mailbox = FakeMailbox(messages=messages)

    out = service.execute_run(
        mailbox,
        [
            {"message_id": "m1", "folder_id": "f1"},
            {"message_id": "m2"},  # missing folder_id -> failure, not fatal
        ],
    )

    assert out["applied"] is True
    assert out["moved_count"] == 1 and out["failed_count"] == 1
    assert mailbox.moved_messages == [("m1", "f1")]
    assert out["failed"][0]["message_id"] == "m2"


def test_execute_run_empty_is_noop() -> None:
    mailbox = FakeMailbox()
    out = service.execute_run(mailbox, [])
    assert out["applied"] is False and out["moved_count"] == 0
    assert mailbox.moved_messages == []


def test_load_ruleset_requires_exactly_one_source() -> None:
    with pytest.raises(ValueError):
        service.load_ruleset()
    with pytest.raises(ValueError):
        service.load_ruleset(rules_json="{}", rules_json_path="x.json")

    ruleset = service.load_ruleset(
        rules_json='{"rules": [{"name": "X", "actions": {"moveToFolder": "F"}}]}'
    )
    assert ruleset.rules[0].name == "X"
