"""Compare parsed rules against the live mailbox and decide what to do with each.

The decision is deliberately conservative: when in doubt we prefer the
non-destructive choice (CREATE over REPLACE, flag-and-skip over silent rewrite).
The planner and CLI turn these decisions into an ordered, confirmable plan.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from rwz_importer.api.owa_client import ExistingRule, normalize_folder_name
from rwz_importer.models import Condition, Rule


class Decision(enum.Enum):
    CREATE = "CREATE"
    SKIP = "SKIP"
    REPLACE = "REPLACE"
    SHADOW_RISK = "SHADOW_RISK"
    FOLDER_MISSING = "FOLDER_MISSING"


@dataclass
class ConflictDecision:
    rule: Rule
    decision: Decision
    reason: str
    folder_missing: bool = False
    existing: ExistingRule | None = None
    shadowed_by: ExistingRule | None = None


def _existing_signature(rule: ExistingRule) -> tuple:
    return Condition(
        fromAddresses=list(rule.from_addresses),
        subjectContains=list(rule.subject_contains),
        senderContains=list(rule.sender_contains),
    ).signature()


def _is_shadowed_by(existing: ExistingRule, rule: Rule) -> bool:
    """True if ``existing`` (a stop-rule) would catch mail this rule targets first.

    The classic case: a broad ``SenderContains: ["@example.org"]`` stop-rule sitting
    above a specific ``FromAddresses: ["alice.sample@example.org"]`` rule — the broad
    one fires and stops processing, so the specific rule never runs.
    """
    if not existing.stop_processing:
        return False
    targets = [a.lower() for a in rule.conditions.fromAddresses]
    for addr in targets:
        for needle in existing.sender_contains:
            if needle and needle.lower() in addr:
                return True
    return False


def detect_conflicts(
    rules: list[Rule],
    existing_rules: list[ExistingRule],
    existing_folder_names: set[str] | None = None,
) -> list[ConflictDecision]:
    """Classify each parsed rule against the current server state.

    ``existing_folder_names`` is a set of *lower-cased* folder display names used
    to flag a missing destination folder (normalised for the en/em-dash case).
    """
    folder_names = existing_folder_names or set()
    by_name: dict[str, ExistingRule] = {r.display_name.strip().lower(): r for r in existing_rules}
    by_sig: dict[tuple, ExistingRule] = {_existing_signature(r): r for r in existing_rules}

    decisions: list[ConflictDecision] = []
    for rule in rules:
        folder = rule.actions.moveToFolder or ""
        folder_missing = bool(folder) and not _folder_present(folder, folder_names)

        name_match = by_name.get(rule.name.strip().lower())
        sig = rule.conditions.signature()
        sig_match = by_sig.get(sig)

        if name_match and _existing_signature(name_match) == sig:
            decision = ConflictDecision(
                rule, Decision.SKIP, "Identical rule already exists.", folder_missing, name_match
            )
        elif sig_match and sig_match.display_name.strip().lower() == rule.name.strip().lower():
            decision = ConflictDecision(
                rule, Decision.SKIP, "Identical rule already exists.", folder_missing, sig_match
            )
        elif name_match:
            decision = ConflictDecision(
                rule,
                Decision.REPLACE,
                "A rule with this name exists but its conditions differ.",
                folder_missing,
                name_match,
            )
        else:
            shadower = next(
                (e for e in existing_rules if _is_shadowed_by(e, rule)), None
            )
            if shadower:
                decision = ConflictDecision(
                    rule,
                    Decision.SHADOW_RISK,
                    f"Existing stop-rule {shadower.display_name!r} (seq {shadower.sequence}) "
                    f"would catch this rule's mail first.",
                    folder_missing,
                    shadowed_by=shadower,
                )
            else:
                decision = ConflictDecision(
                    rule, Decision.CREATE, "No matching rule on the server.", folder_missing
                )
        decisions.append(decision)
    return decisions


def _folder_present(display_name: str, folder_names: set[str]) -> bool:
    candidates = {
        display_name.strip().lower(),
        normalize_folder_name(display_name).strip().lower(),
    }
    return bool(candidates & folder_names)
