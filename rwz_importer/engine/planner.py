"""Turn per-rule conflict decisions into an explicit, ordered apply plan.

The plan is what ``apply`` prints in its dry-run and executes (in order) with
``--yes``: which folders to create, which rules to create/replace/skip, and at
which sequence. Transforms the user asked for (exclusions, address remaps) are
applied here so the plan reflects exactly what will hit the server.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from rwz_importer.api.owa_client import normalize_folder_name
from rwz_importer.engine.conflict import ConflictDecision, Decision
from rwz_importer.models import Rule


class ActionKind(enum.Enum):
    CREATE_FOLDER = "CREATE_FOLDER"
    CREATE_RULE = "CREATE_RULE"
    REPLACE_RULE = "REPLACE_RULE"
    SKIP_RULE = "SKIP_RULE"


@dataclass
class PlannedAction:
    kind: ActionKind
    reason: str
    rule: Rule | None = None
    folder_display: str | None = None
    folder_create_name: str | None = None
    sequence: int | None = None
    existing_rule_id: str | None = None
    needs_review: bool = False


@dataclass
class ApplyPlan:
    actions: list[PlannedAction] = field(default_factory=list)

    @property
    def folder_creations(self) -> list[PlannedAction]:
        return [a for a in self.actions if a.kind is ActionKind.CREATE_FOLDER]

    @property
    def rule_creations(self) -> list[PlannedAction]:
        return [a for a in self.actions if a.kind in (ActionKind.CREATE_RULE, ActionKind.REPLACE_RULE)]

    @property
    def skips(self) -> list[PlannedAction]:
        return [a for a in self.actions if a.kind is ActionKind.SKIP_RULE]


def build_plan(
    decisions: list[ConflictDecision],
    *,
    excludes: set[str] | None = None,
    address_map: dict[str, str] | None = None,
    normalize_folders: bool = True,
    base_sequence: int = 1,
) -> ApplyPlan:
    """Build an :class:`ApplyPlan` from conflict decisions and user transforms.

    ``excludes`` is a set of lower-cased rule names to drop; ``address_map`` maps
    a lower-cased source address to its replacement. A rule that still carries a
    cross-mailbox-flagged address with no mapping is marked ``needs_review`` (the
    CLI prompts, or skips it under ``--yes`` — it is never silently rewritten).
    """
    excludes = {e.strip().lower() for e in (excludes or set())}
    address_map = {k.strip().lower(): v for k, v in (address_map or {}).items()}

    plan = ApplyPlan()
    scheduled_folders: set[str] = set()
    seq = base_sequence

    for dec in decisions:
        rule = dec.rule
        if rule.exclude or rule.name.strip().lower() in excludes:
            plan.actions.append(
                PlannedAction(ActionKind.SKIP_RULE, "Excluded by user.", rule=rule)
            )
            continue

        _apply_address_map(rule, address_map)

        if dec.decision is Decision.SKIP:
            plan.actions.append(
                PlannedAction(ActionKind.SKIP_RULE, dec.reason, rule=rule, existing_rule_id=_eid(dec))
            )
            continue

        # Schedule folder creation once per distinct destination.
        if dec.folder_missing and rule.actions.moveToFolder:
            display = rule.actions.moveToFolder
            create_name = normalize_folder_name(display) if normalize_folders else display
            if create_name.strip().lower() not in scheduled_folders:
                scheduled_folders.add(create_name.strip().lower())
                plan.actions.append(
                    PlannedAction(
                        ActionKind.CREATE_FOLDER,
                        f"Destination folder {display!r} is missing.",
                        folder_display=display,
                        folder_create_name=create_name,
                    )
                )

        review = _has_unresolved_cross_mailbox(rule)
        if dec.decision is Decision.SHADOW_RISK and dec.shadowed_by is not None:
            assigned = max(1, dec.shadowed_by.sequence or seq)
        else:
            assigned = seq
            seq += 1

        kind = ActionKind.REPLACE_RULE if dec.decision is Decision.REPLACE else ActionKind.CREATE_RULE
        plan.actions.append(
            PlannedAction(
                kind,
                dec.reason,
                rule=rule,
                folder_display=rule.actions.moveToFolder,
                sequence=assigned,
                existing_rule_id=_eid(dec),
                needs_review=review,
            )
        )
    return plan


def _eid(dec: ConflictDecision) -> str | None:
    return dec.existing.id if dec.existing else None


def _apply_address_map(rule: Rule, address_map: dict[str, str]) -> None:
    if not address_map:
        return
    remapped = False
    new_addresses = []
    for addr in rule.conditions.fromAddresses:
        replacement = address_map.get(addr.strip().lower())
        if replacement is not None and replacement != addr:
            remapped = True
            new_addresses.append(replacement)
        else:
            new_addresses.append(addr)
    rule.conditions.fromAddresses = new_addresses
    # An explicit remap resolves the cross-mailbox concern for that address.
    if remapped:
        rule.flags.addressMayBelongToDifferentMailbox = False


def _has_unresolved_cross_mailbox(rule: Rule) -> bool:
    """A flagged rule still needs manual review (its source-mailbox address remains)."""
    return rule.flags.addressMayBelongToDifferentMailbox
