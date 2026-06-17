"""Conflict-detector tests: the four decisions that drive a safe apply plan."""

from __future__ import annotations

from rwz_importer.api.owa_client import ExistingRule
from rwz_importer.engine import Decision, detect_conflicts
from rwz_importer.models import Action, Condition, Rule


def test_shadow_risk_detected() -> None:
    """A specific from-address rule sitting under a broad ``@example.org`` stop-rule."""
    rule = Rule(
        name="Email LabOps",
        conditions=Condition(fromAddresses=["alice.sample@example.org"]),
        actions=Action(moveToFolder="Email LabOps"),
    )
    existing = [
        ExistingRule(
            id="r1",
            display_name="EXAMPLE.ORG catch-all",
            sequence=4,
            sender_contains=["@EXAMPLE.ORG"],
            stop_processing=True,
        )
    ]
    decisions = detect_conflicts([rule], existing, {"email labops"})
    assert decisions[0].decision is Decision.SHADOW_RISK
    assert decisions[0].shadowed_by is existing[0]


def test_exact_duplicate_skipped() -> None:
    rule = Rule(
        name="Confluence",
        conditions=Condition(fromAddresses=["confluence-noreply@example.com"]),
        actions=Action(moveToFolder="Confluence"),
    )
    existing = [
        ExistingRule(
            id="r1",
            display_name="Confluence",
            from_addresses=["confluence-noreply@example.com"],
        )
    ]
    decisions = detect_conflicts([rule], existing, {"confluence"})
    assert decisions[0].decision is Decision.SKIP


def test_partial_match_replace_offered() -> None:
    """Same name, narrower server conditions -> REPLACE (not a silent skip)."""
    rule = Rule(
        name="Accepted Meetings",
        conditions=Condition(subjectContains=["Accepted:", "Tentative:"]),
        actions=Action(moveToFolder="Meeting Invites"),
    )
    existing = [
        ExistingRule(id="r1", display_name="Accepted Meetings", subject_contains=["Accepted:"])
    ]
    decisions = detect_conflicts([rule], existing, {"meeting invites"})
    assert decisions[0].decision is Decision.REPLACE
    assert decisions[0].existing is existing[0]


def test_no_conflict_creates() -> None:
    rule = Rule(
        name="Brand New",
        conditions=Condition(fromAddresses=["new@example.com"]),
        actions=Action(moveToFolder="New Folder"),
    )
    decisions = detect_conflicts([rule], [], set())
    assert decisions[0].decision is Decision.CREATE
    assert decisions[0].folder_missing is True
