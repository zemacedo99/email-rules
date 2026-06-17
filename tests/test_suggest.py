"""Suggestion-engine tests: pattern detection over synthetic message metadata."""

from __future__ import annotations

from rwz_importer.api.owa_client import ExistingRule, Message
from rwz_importer.engine.suggest import suggest_rules


def _msg(addr: str, subject: str = "hello", i: int = 0) -> Message:
    return Message(id=f"m{i}", subject=subject, sender_address=addr, sender_name=addr)


def test_high_volume_sender_suggested() -> None:
    messages = [_msg("newsletter@shop.com", "Daily deal", i) for i in range(5)]
    out = suggest_rules(messages, [], min_sender=3)
    match = next(s for s in out if s.condition.fromAddresses == ["newsletter@shop.com"])
    assert match.message_count == 5
    assert match.suggested_folder  # a non-empty folder is proposed


def test_covered_sender_not_suggested() -> None:
    messages = [_msg("ci@build.com", "Build #%d" % i, i) for i in range(6)]
    existing = [ExistingRule(id="r1", display_name="CI", from_addresses=["ci@build.com"])]
    out = suggest_rules(messages, existing, min_sender=3)
    assert all("ci@build.com" not in (s.condition.fromAddresses or [""])[0] for s in out)


def test_sender_covered_by_domain_needle_not_suggested() -> None:
    messages = [_msg("anyone@corp.io", "x", i) for i in range(5)]
    existing = [ExistingRule(id="r1", display_name="Corp", sender_contains=["@corp.io"])]
    out = suggest_rules(messages, existing, min_sender=3, min_domain=3)
    assert out == []


def test_domain_cluster_suggested() -> None:
    messages = [_msg(f"{local}@corp.io", "note", i) for i, local in enumerate("abcdef")]
    # Disable per-sender (each sender appears once) so only the domain rule can fire.
    out = suggest_rules(messages, [], min_sender=99, min_domain=6)
    domain = next(s for s in out if s.condition.senderContains == ["@corp.io"])
    assert domain.message_count == 6


def test_subject_tag_suggested() -> None:
    messages = [_msg(f"u{i}@x.com", "[JIRA] ticket moved", i) for i in range(4)]
    out = suggest_rules(messages, [], min_sender=99, min_domain=99, min_subject=4)
    tag = next(s for s in out if s.condition.subjectContains)
    assert tag.condition.subjectContains == ["JIRA"]
    assert tag.message_count == 4


def test_suggestion_to_rule_roundtrips() -> None:
    messages = [_msg("news@shop.com", "Deal", i) for i in range(3)]
    out = suggest_rules(messages, [], min_sender=3)
    rule = out[0].to_rule()
    assert rule.conditions.fromAddresses == ["news@shop.com"]
    assert rule.actions.moveToFolder
    assert rule.actions.stopProcessingRules is True


def test_below_threshold_not_suggested() -> None:
    messages = [_msg("rare@x.com", "hi", i) for i in range(2)]
    out = suggest_rules(messages, [], min_sender=3)
    assert out == []
