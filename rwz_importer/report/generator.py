"""Render the live mailbox state (and optional validation) as Markdown or JSON.

The ``report`` command fetches current folders + inbox rules and hands them here.
``apply`` can also pass a :class:`ValidationResult` so the report records whether
the just-applied state matches what was expected.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from rwz_importer.api.owa_client import ExistingRule, Folder
from rwz_importer.engine.validator import ValidationResult


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _folder_name_map(folders: list[Folder]) -> dict[str, str]:
    return {f.id: f.display_name for f in folders}


def _rule_to_dict(rule: ExistingRule, folder_names: dict[str, str]) -> dict:
    return {
        "sequence": rule.sequence,
        "name": rule.display_name,
        "enabled": rule.is_enabled,
        "conditions": {
            "fromAddresses": rule.from_addresses,
            "subjectContains": rule.subject_contains,
            "senderContains": rule.sender_contains,
        },
        "moveToFolder": folder_names.get(rule.move_to_folder_id or "", rule.move_to_folder_id),
        "stopProcessingRules": rule.stop_processing,
    }


def generate_json_report(
    rules: list[ExistingRule],
    folders: list[Folder],
    *,
    mailbox: str | None = None,
    validation: ValidationResult | None = None,
) -> str:
    """Return the current state as indented JSON text."""
    folder_names = _folder_name_map(folders)
    payload: dict = {
        "mailbox": mailbox,
        "generated_at": _now_iso(),
        "folders": sorted(f.display_name for f in folders),
        "rules": [
            _rule_to_dict(r, folder_names)
            for r in sorted(rules, key=lambda r: (r.sequence is None, r.sequence or 0))
        ],
    }
    if validation is not None:
        payload["validation"] = {
            "ok": validation.ok,
            "summary": validation.summary,
            "rules": [
                {"name": s.name, "ok": s.ok, "detail": s.detail} for s in validation.statuses
            ],
            "unexpected_rules": validation.extra_rules,
        }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def generate_markdown_report(
    rules: list[ExistingRule],
    folders: list[Folder],
    *,
    mailbox: str | None = None,
    validation: ValidationResult | None = None,
) -> str:
    """Return the current state as a Markdown document."""
    folder_names = _folder_name_map(folders)
    ordered = sorted(rules, key=lambda r: (r.sequence is None, r.sequence or 0))

    lines: list[str] = ["# Outlook Rules Report", ""]
    if mailbox:
        lines.append(f"- **Mailbox:** {mailbox}")
    lines.append(f"- **Generated:** {_now_iso()}")
    lines.append(f"- **Rules:** {len(ordered)}  **Folders:** {len(folders)}")
    lines.append("")

    lines.append("## Inbox Rules")
    lines.append("")
    lines.append("| Seq | Name | Conditions | Move to | Stop |")
    lines.append("|----:|------|------------|---------|:----:|")
    for r in ordered:
        conds = _format_conditions(r)
        dest = folder_names.get(r.move_to_folder_id or "", r.move_to_folder_id or "—")
        stop = "yes" if r.stop_processing else "no"
        lines.append(f"| {r.sequence if r.sequence is not None else '—'} | {r.display_name} | {conds} | {dest} | {stop} |")
    lines.append("")

    lines.append("## Folders")
    lines.append("")
    for name in sorted(f.display_name for f in folders):
        lines.append(f"- {name}")
    lines.append("")

    if validation is not None:
        lines.append("## Validation")
        lines.append("")
        lines.append(f"**{validation.summary}**")
        lines.append("")
        lines.append("| Rule | Status | Detail |")
        lines.append("|------|:------:|--------|")
        for s in validation.statuses:
            lines.append(f"| {s.name} | {'OK' if s.ok else 'FAIL'} | {s.detail} |")
        if validation.extra_rules:
            lines.append("")
            lines.append("_Unexpected rules on server:_ " + ", ".join(validation.extra_rules))
        lines.append("")

    return "\n".join(lines)


def _format_conditions(rule: ExistingRule) -> str:
    parts: list[str] = []
    if rule.from_addresses:
        parts.append("from: " + ", ".join(rule.from_addresses))
    if rule.subject_contains:
        parts.append("subject: " + ", ".join(rule.subject_contains))
    if rule.sender_contains:
        parts.append("sender: " + ", ".join(rule.sender_contains))
    return "; ".join(parts) if parts else "—"
