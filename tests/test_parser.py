"""Parser tests pinned to the real ``sample_rules.rwz`` fixture.

These lock in the validated reading of the binary: 13 rules, clean addresses
(no ``SMTP:``/X.500 leakage), exact subject keywords, the cross-mailbox flag, and
the merged Weekly Digest & Viva Engage rule.
"""

from __future__ import annotations

from rwz_importer.models import RuleSet

EXPECTED_NAMES = {
    "Meeting Invites",
    "SI HelpDesk",
    "Confluence",
    "Email LabOps",
    "ACMESUP",
    "PRJX",
    "org daily stuff",
    "Bitbucket",
    "Report",
    "Sharepoint",
    "Training",
    "Teams",
    "Weekly Digest & Viva Engage",
}


def _by_name(ruleset: RuleSet, name: str):
    return next(r for r in ruleset.rules if r.name == name)


def test_parse_fixture_rule_count(parsed_ruleset: RuleSet) -> None:
    assert len(parsed_ruleset.rules) == 13


def test_parse_rule_names(parsed_ruleset: RuleSet) -> None:
    assert {r.name for r in parsed_ruleset.rules} == EXPECTED_NAMES


def test_smtp_prefix_stripped(parsed_ruleset: RuleSet) -> None:
    for rule in parsed_ruleset.rules:
        for addr in rule.conditions.fromAddresses:
            assert not addr.lower().startswith("smtp:"), addr


def test_x500_filtered(parsed_ruleset: RuleSet) -> None:
    for rule in parsed_ruleset.rules:
        for value in (
            rule.conditions.fromAddresses
            + rule.conditions.subjectContains
            + rule.conditions.senderContains
        ):
            lowered = value.lower()
            assert "/o=" not in lowered
            assert "/cn=" not in lowered
            assert not lowered.startswith("ex:")


def test_meeting_invites_conditions(parsed_ruleset: RuleSet) -> None:
    rule = _by_name(parsed_ruleset, "Meeting Invites")
    assert rule.conditions.subjectContains == ["Accepted:", "Tentative:", "Aceite:"]
    assert rule.conditions.fromAddresses == []


def test_cross_mailbox_flag(parsed_ruleset: RuleSet) -> None:
    labops = _by_name(parsed_ruleset, "Email LabOps")
    assert labops.conditions.fromAddresses == ["alice.sample@example.org"]
    assert labops.flags.addressMayBelongToDifferentMailbox is True
    # A same-domain address must not be flagged.
    helpdesk = _by_name(parsed_ruleset, "SI HelpDesk")
    assert helpdesk.flags.addressMayBelongToDifferentMailbox is False


def test_weekly_digest_multi_from(parsed_ruleset: RuleSet) -> None:
    rule = _by_name(parsed_ruleset, "Weekly Digest & Viva Engage")
    assert sorted(rule.conditions.fromAddresses) == [
        "digest-noreply@engage.mail.invalid",
        "digest-notify@engage.mail.invalid",
    ]
    assert rule.actions.moveToFolder == "Weekly Digest"
