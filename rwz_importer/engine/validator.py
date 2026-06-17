"""Post-apply validation: compare the live mailbox state against what we expected.

Used by ``apply`` (automatically, after writing) and by the standalone
``validate`` command. It answers one question per expected rule: is a rule with
this name and these conditions actually present on the server now?
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rwz_importer.api.owa_client import ExistingRule
from rwz_importer.models import Condition, Rule, RuleSet


@dataclass
class RuleStatus:
    name: str
    present: bool
    conditions_match: bool
    detail: str

    @property
    def ok(self) -> bool:
        return self.present and self.conditions_match


@dataclass
class ValidationResult:
    statuses: list[RuleStatus] = field(default_factory=list)
    extra_rules: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.statuses)

    @property
    def summary(self) -> str:
        good = sum(1 for s in self.statuses if s.ok)
        return f"{good}/{len(self.statuses)} expected rules present and correct"


def _signature_of_existing(rule: ExistingRule) -> tuple:
    return Condition(
        fromAddresses=list(rule.from_addresses),
        subjectContains=list(rule.subject_contains),
        senderContains=list(rule.sender_contains),
    ).signature()


def validate_state(live_rules: list[ExistingRule], expected: RuleSet) -> ValidationResult:
    """Check every non-excluded expected rule against the live mailbox rules."""
    by_name: dict[str, ExistingRule] = {r.display_name.strip().lower(): r for r in live_rules}
    matched_ids: set[str] = set()
    result = ValidationResult()

    for rule in expected.rules:
        if rule.exclude:
            continue
        found = by_name.get(rule.name.strip().lower())
        if found is None:
            result.statuses.append(
                RuleStatus(rule.name, False, False, "Not found on server.")
            )
            continue
        matched_ids.add(found.id)
        if _signature_of_existing(found) == rule.conditions.signature():
            result.statuses.append(RuleStatus(rule.name, True, True, "OK"))
        else:
            result.statuses.append(
                RuleStatus(
                    rule.name,
                    True,
                    False,
                    f"Conditions differ (server from={found.from_addresses}, "
                    f"subject={found.subject_contains}).",
                )
            )

    expected_names = {r.name.strip().lower() for r in expected.rules if not r.exclude}
    for live in live_rules:
        if live.display_name.strip().lower() not in expected_names:
            result.extra_rules.append(live.display_name)
    return result
