"""Programmatic, UI-free facade over the rwz-importer pipeline.

This is the layer an *agent* drives (through :mod:`rwz_importer.mcp_server`) and
that any non-CLI caller can use directly. Every function takes a
:class:`~rwz_importer.api.provider.MailboxProvider` (the OWA client conforms) and
returns plain JSON-able values — no ``click``, no printing, no global state.

The one mutating entry point, :func:`execute_plan`, implements **apply with
confirmation**:

1. The plan and a ``confirmation_token`` (a digest of *exactly what would be
   written*) are computed first.
2. A write happens only when the caller passes ``confirm=True`` **and** echoes
   back a token that still matches the freshly recomputed plan.
3. If the mailbox drifted since the plan was shown, the recomputed token differs,
   the write is refused, and the caller is handed the new plan + token to
   re-confirm.

Cross-mailbox-flagged addresses are converted to *skips* here (never written
silently); resolve them up front with ``address_map``. Existing rules are deleted
only as part of a confirmed ``REPLACE``; folders are never deleted.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from rwz_importer.api.owa_client import (
    ExistingRule,
    Folder,
    normalize_folder_name,
)
from rwz_importer.api.provider import MailboxProvider
from rwz_importer.engine import (
    ActionKind,
    ApplyPlan,
    RuleSuggestion,
    build_plan,
    detect_conflicts,
    suggest_rules,
    validate_state,
)
from rwz_importer.models import RuleSet
from rwz_importer.parser import parse_rwz_file
from rwz_importer.report.generator import generate_json_report, generate_markdown_report

__all__ = [
    "load_ruleset",
    "parse_rules",
    "list_folders",
    "list_rules",
    "read_messages",
    "suggest",
    "report",
    "compute_plan",
    "execute_plan",
]

_ACTION_VERB = {
    ActionKind.CREATE_RULE: "CREATE",
    ActionKind.REPLACE_RULE: "REPLACE",
    ActionKind.SKIP_RULE: "SKIP",
}


# ----------------------------------------------------------------- ruleset IO --
def load_ruleset(
    *,
    file_path: str | Path | None = None,
    rules_json_path: str | Path | None = None,
    rules_json: str | None = None,
) -> RuleSet:
    """Build a :class:`RuleSet` from exactly one source.

    ``file_path`` parses a ``.rwz`` export; ``rules_json_path`` reads a previously
    parsed (and possibly edited) JSON file; ``rules_json`` is that same JSON as an
    inline string. Exactly one must be supplied.
    """
    provided = [x for x in (file_path, rules_json_path, rules_json) if x]
    if len(provided) != 1:
        raise ValueError(
            "Provide exactly one of file_path, rules_json_path, or rules_json."
        )
    if file_path:
        return parse_rwz_file(file_path)
    if rules_json_path:
        return RuleSet.from_json(Path(rules_json_path).read_text(encoding="utf-8"))
    return RuleSet.from_json(rules_json or "")


def parse_rules(file_path: str | Path) -> dict:
    """Parse a ``.rwz`` file into the canonical rules dict. Offline and read-only."""
    ruleset = parse_rwz_file(file_path)
    data = ruleset.to_dict()
    data["rule_count"] = len(ruleset.rules)
    data["flagged_cross_mailbox"] = [
        r.name for r in ruleset.rules if r.flags.addressMayBelongToDifferentMailbox
    ]
    return data


# ---------------------------------------------------------------- read-only --
def list_folders(provider: MailboxProvider) -> list[dict]:
    """Return every mail folder (Inbox children + top level), deduped by id."""
    return [{"id": f.id, "display_name": f.display_name} for f in _collect_folders(provider)]


def list_rules(provider: MailboxProvider) -> list[dict]:
    """Return the inbox message rules currently on the server, ordered by sequence."""
    rules = sorted(
        provider.list_rules(), key=lambda r: (r.sequence is None, r.sequence or 0)
    )
    return [_existing_rule_view(r) for r in rules]


def read_messages(
    provider: MailboxProvider, *, folder: str = "Inbox", top: int = 200
) -> dict:
    """Read recent message *metadata* (sender/subject/date — never bodies)."""
    messages = provider.list_messages(folder=folder, top=top)
    return {
        "folder": folder,
        "count": len(messages),
        "messages": [
            {
                "subject": m.subject,
                "sender_address": m.sender_address,
                "sender_name": m.sender_name,
                "received": m.received,
            }
            for m in messages
        ],
    }


def suggest(
    provider: MailboxProvider,
    *,
    folder: str = "Inbox",
    top: int = 300,
    min_sender: int = 3,
    min_domain: int = 6,
    min_subject: int = 4,
    max_suggestions: int = 20,
) -> dict:
    """Read recent mail and propose rules for unfiled patterns. Read-only.

    Returns advisory suggestions only; feed an accepted one back through
    :func:`compute_plan` / :func:`execute_plan` to apply it.
    """
    messages = provider.list_messages(folder=folder, top=top)
    existing = provider.list_rules()
    suggestions = suggest_rules(
        messages,
        existing,
        min_sender=min_sender,
        min_domain=min_domain,
        min_subject=min_subject,
        max_suggestions=max_suggestions,
    )
    return {
        "folder": folder,
        "sampled_messages": len(messages),
        "suggestion_count": len(suggestions),
        "suggestions": [_suggestion_view(s) for s in suggestions],
    }


def report(
    provider: MailboxProvider, *, fmt: str = "markdown", mailbox: str | None = None
) -> str:
    """Render the live mailbox state (rules + folders) as Markdown or JSON text."""
    rules = provider.list_rules()
    folders = _collect_folders(provider)
    if fmt == "json":
        return generate_json_report(rules, folders, mailbox=mailbox)
    return generate_markdown_report(rules, folders, mailbox=mailbox)


# ------------------------------------------------------- plan (read-only) --
def compute_plan(
    provider: MailboxProvider,
    ruleset: RuleSet,
    *,
    excludes: set[str] | None = None,
    address_map: dict[str, str] | None = None,
    normalize_folders: bool = True,
    mailbox: str | None = None,
) -> dict:
    """Compute the dry-run plan + ``confirmation_token`` without writing anything.

    The returned ``confirmation_token`` is what :func:`execute_plan` requires to
    actually apply: it is a digest of precisely the folders/rules that would be
    written, so it changes if the mailbox drifts.
    """
    plan, existing_rules, _folders, skipped = _build_resolved_plan(
        provider, ruleset, excludes, address_map, normalize_folders
    )
    return _plan_view(
        plan,
        existing_rules=existing_rules,
        skipped_for_review=skipped,
        mailbox=mailbox,
    )


# --------------------------------------------------------- apply (write) --
def execute_plan(
    provider: MailboxProvider,
    ruleset: RuleSet,
    *,
    excludes: set[str] | None = None,
    address_map: dict[str, str] | None = None,
    normalize_folders: bool = True,
    confirm: bool = False,
    confirmation_token: str | None = None,
    mailbox: str | None = None,
) -> dict:
    """Apply the plan to the mailbox — but only with explicit confirmation.

    Returns a dry-run preview (``applied: False``) unless ``confirm=True`` and the
    supplied ``confirmation_token`` still matches the freshly recomputed plan. A
    mismatch means the mailbox changed since the plan was shown; the write is
    refused and a new token is returned to re-confirm against.
    """
    plan, existing_rules, folders, skipped = _build_resolved_plan(
        provider, ruleset, excludes, address_map, normalize_folders
    )
    view = _plan_view(
        plan, existing_rules=existing_rules, skipped_for_review=skipped, mailbox=mailbox
    )
    token = view["confirmation_token"]

    if not confirm:
        view["applied"] = False
        view["status"] = "dry_run"
        view["message"] = (
            "Dry run only — nothing was written. To apply, call again with "
            "confirm=true and confirmation_token set to the value shown here."
        )
        return view

    if not confirmation_token or confirmation_token != token:
        view["applied"] = False
        view["status"] = "confirmation_required"
        view["message"] = (
            "Not applied: the confirmation_token was missing or no longer matches "
            "(the mailbox may have changed since the plan was computed). Review the "
            "plan above and re-call with confirm=true and this confirmation_token."
        )
        return view

    result = _execute(provider, plan, folders)
    validation = validate_state(provider.list_rules(), ruleset)
    result["applied"] = True
    result["status"] = "applied"
    result["confirmation_token"] = token
    result["validation"] = _validation_view(validation)
    result["skipped_for_review"] = skipped
    return result


# --------------------------------------------------------------------- helpers --
def _build_resolved_plan(
    provider: MailboxProvider,
    ruleset: RuleSet,
    excludes: set[str] | None,
    address_map: dict[str, str] | None,
    normalize_folders: bool,
) -> tuple[ApplyPlan, list[ExistingRule], list[Folder], list[str]]:
    """Detect conflicts, build the plan, and resolve cross-mailbox flags to skips."""
    existing_rules = provider.list_rules()
    folders = _collect_folders(provider)
    folder_names = {f.display_name.strip().lower() for f in folders if f.display_name}

    decisions = detect_conflicts(ruleset.rules, existing_rules, folder_names)
    plan = build_plan(
        decisions,
        excludes=excludes,
        address_map=address_map,
        normalize_folders=normalize_folders,
        base_sequence=_next_sequence(existing_rules),
    )
    skipped = _resolve_flagged(plan)
    return plan, existing_rules, folders, skipped


def _resolve_flagged(plan: ApplyPlan) -> list[str]:
    """Convert any unresolved cross-mailbox rule to a skip; return their names.

    Mirrors the CLI's non-interactive ``--yes`` behaviour: a flagged address is
    never written without an explicit ``address_map`` entry.
    """
    skipped: list[str] = []
    for action in plan.actions:
        if action.needs_review and action.rule is not None:
            action.kind = ActionKind.SKIP_RULE
            action.needs_review = False
            action.reason = "Skipped: unresolved cross-mailbox address (use address_map)."
            skipped.append(action.rule.name)
    return skipped


def _execute(provider: MailboxProvider, plan: ApplyPlan, existing_folders: list[Folder]) -> dict:
    """Create folders, then create/replace rules. Deletes only inside a REPLACE."""
    folder_ids: dict[str, str] = {}
    for folder in existing_folders:
        if folder.display_name:
            folder_ids[folder.display_name.strip().lower()] = folder.id
            folder_ids[normalize_folder_name(folder.display_name).strip().lower()] = folder.id

    created_folders: list[str] = []
    for action in plan.folder_creations:
        name = action.folder_create_name or action.folder_display or ""
        created = provider.create_folder(name)
        folder_ids[created.display_name.strip().lower()] = created.id
        if action.folder_display:
            folder_ids[action.folder_display.strip().lower()] = created.id
        created_folders.append(created.display_name)

    created_rules: list[dict] = []
    replaced_rules: list[str] = []
    for action in plan.actions:
        if action.kind not in (ActionKind.CREATE_RULE, ActionKind.REPLACE_RULE):
            continue
        rule = action.rule
        assert rule is not None
        folder_id = _resolve_folder_id(action.folder_display, folder_ids)

        if action.kind is ActionKind.REPLACE_RULE and action.existing_rule_id:
            provider.delete_rule(action.existing_rule_id)
            replaced_rules.append(rule.name)

        provider.create_rule(
            display_name=rule.name,
            sequence=action.sequence or 1,
            folder_id=folder_id,
            from_addresses=rule.conditions.fromAddresses or None,
            subject_contains=rule.conditions.subjectContains or None,
            sender_contains=rule.conditions.senderContains or None,
            stop_processing=rule.actions.stopProcessingRules,
            enabled=rule.enabled,
        )
        created_rules.append({"name": rule.name, "sequence": action.sequence})

    return {
        "created_folders": created_folders,
        "created_rules": created_rules,
        "replaced_rules": replaced_rules,
    }


def _resolve_folder_id(display: str | None, folder_ids: dict[str, str]) -> str | None:
    if not display:
        return None
    for key in (display.strip().lower(), normalize_folder_name(display).strip().lower()):
        if key in folder_ids:
            return folder_ids[key]
    return None


def _collect_folders(provider: MailboxProvider) -> list[Folder]:
    seen: dict[str, Folder] = {}
    for folder in provider.list_inbox_folders() + provider.list_all_folders():
        if folder.id and folder.id not in seen:
            seen[folder.id] = folder
    return list(seen.values())


def _next_sequence(existing_rules: list[ExistingRule]) -> int:
    seqs = [r.sequence for r in existing_rules if r.sequence is not None]
    return max(seqs) + 1 if seqs else 1


def _plan_view(
    plan: ApplyPlan,
    *,
    existing_rules: list[ExistingRule],
    skipped_for_review: list[str],
    mailbox: str | None = None,
) -> dict:
    folders_to_create = [
        {
            "display_name": a.folder_display,
            "create_as": a.folder_create_name or a.folder_display,
        }
        for a in plan.folder_creations
    ]
    rules: list[dict] = []
    for a in plan.actions:
        if a.kind is ActionKind.CREATE_FOLDER:
            continue
        rule = a.rule
        rules.append(
            {
                "action": _ACTION_VERB[a.kind],
                "name": rule.name if rule else "?",
                "sequence": a.sequence,
                "move_to_folder": a.folder_display,
                "conditions": rule.conditions.to_dict() if rule else {},
                "stop_processing": rule.actions.stopProcessingRules if rule else None,
                "reason": a.reason,
                "replaces_rule_id": a.existing_rule_id
                if a.kind is ActionKind.REPLACE_RULE
                else None,
            }
        )

    counts = {
        "create": sum(1 for r in rules if r["action"] == "CREATE"),
        "replace": sum(1 for r in rules if r["action"] == "REPLACE"),
        "skip": sum(1 for r in rules if r["action"] == "SKIP"),
        "folders": len(folders_to_create),
    }
    return {
        "mailbox": mailbox,
        "existing_rule_count": len(existing_rules),
        "folders_to_create": folders_to_create,
        "rules": rules,
        "summary": counts,
        "skipped_for_review": skipped_for_review,
        "confirmation_token": _plan_digest(plan),
    }


def _validation_view(result) -> dict:
    return {
        "ok": result.ok,
        "summary": result.summary,
        "rules": [{"name": s.name, "ok": s.ok, "detail": s.detail} for s in result.statuses],
        "unexpected_rules": list(result.extra_rules),
    }


def _existing_rule_view(rule: ExistingRule) -> dict:
    return {
        "id": rule.id,
        "name": rule.display_name,
        "sequence": rule.sequence,
        "enabled": rule.is_enabled,
        "conditions": {
            "fromAddresses": list(rule.from_addresses),
            "subjectContains": list(rule.subject_contains),
            "senderContains": list(rule.sender_contains),
        },
        "move_to_folder_id": rule.move_to_folder_id,
        "stop_processing": rule.stop_processing,
    }


def _suggestion_view(s: RuleSuggestion) -> dict:
    data = s.to_dict()
    data["rule"] = s.to_rule().to_dict()
    return data


def _plan_digest(plan: ApplyPlan) -> str:
    """A stable digest of only the *write-effecting* parts of the plan.

    Folder creations and CREATE/REPLACE rules (with their assigned sequence,
    destination, conditions and flags) are hashed; skips and human-readable
    reasons are excluded. Recomputing against unchanged mailbox state yields the
    same token — any drift changes it, which is what gates a stale apply.
    """
    payload: list = []
    for a in plan.folder_creations:
        payload.append(["FOLDER", (a.folder_create_name or a.folder_display or "").strip().lower()])
    for a in plan.actions:
        if a.kind not in (ActionKind.CREATE_RULE, ActionKind.REPLACE_RULE):
            continue
        rule = a.rule
        assert rule is not None
        payload.append(
            [
                a.kind.value,
                (rule.name or "").strip().lower(),
                a.sequence,
                (a.folder_display or "").strip().lower(),
                sorted(x.strip().lower() for x in rule.conditions.fromAddresses),
                sorted(x.strip().lower() for x in rule.conditions.subjectContains),
                sorted(x.strip().lower() for x in rule.conditions.senderContains),
                bool(rule.actions.stopProcessingRules),
                bool(rule.enabled),
                a.existing_rule_id or "",
            ]
        )
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return "v1:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]
