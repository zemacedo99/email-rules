"""Canonical rule model shared by every stage (parse -> plan -> apply -> validate -> report).

The on-disk JSON shape produced by ``parse`` and consumed by ``apply`` is:

    {
      "source_file": "sample_rules.rwz",
      "source_mailbox": "alice.sample@example.com",
      "extracted_at": "2026-06-17T00:00:00Z",
      "rules": [
        {
          "name": "Weekly Digest & Viva Engage",
          "sequence": 13,
          "enabled": true,
          "exclude": false,
          "conditions": {"fromAddresses": [...], "subjectContains": [], "senderContains": []},
          "actions": {"moveToFolder": "Weekly Digest", "stopProcessingRules": true},
          "flags": {"addressMayBelongToDifferentMailbox": false, "unsupportedConditions": []}
        }
      ]
    }

``exclude`` is an apply-time switch a user can flip by editing the JSON (the "Both"
transforms UX). The parser always emits ``exclude: false``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _norm(values: list[str]) -> tuple[str, ...]:
    """Lower-case, strip and sort a list of strings for order-insensitive comparison."""
    return tuple(sorted(v.strip().lower() for v in values if v and v.strip()))


@dataclass
class Condition:
    fromAddresses: list[str] = field(default_factory=list)
    subjectContains: list[str] = field(default_factory=list)
    senderContains: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.fromAddresses or self.subjectContains or self.senderContains)

    def signature(self) -> tuple:
        """A normalised, hashable representation for conflict comparison."""
        return (
            ("from",) + _norm(self.fromAddresses),
            ("subject",) + _norm(self.subjectContains),
            ("sender",) + _norm(self.senderContains),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fromAddresses": list(self.fromAddresses),
            "subjectContains": list(self.subjectContains),
            "senderContains": list(self.senderContains),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Condition":
        d = d or {}
        return cls(
            fromAddresses=list(d.get("fromAddresses", []) or []),
            subjectContains=list(d.get("subjectContains", []) or []),
            senderContains=list(d.get("senderContains", []) or []),
        )


@dataclass
class Action:
    moveToFolder: str | None = None
    stopProcessingRules: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "moveToFolder": self.moveToFolder,
            "stopProcessingRules": self.stopProcessingRules,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Action":
        d = d or {}
        return cls(
            moveToFolder=d.get("moveToFolder"),
            stopProcessingRules=bool(d.get("stopProcessingRules", True)),
        )


@dataclass
class Flags:
    addressMayBelongToDifferentMailbox: bool = False
    unsupportedConditions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "addressMayBelongToDifferentMailbox": self.addressMayBelongToDifferentMailbox,
            "unsupportedConditions": list(self.unsupportedConditions),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Flags":
        d = d or {}
        return cls(
            addressMayBelongToDifferentMailbox=bool(
                d.get("addressMayBelongToDifferentMailbox", False)
            ),
            unsupportedConditions=list(d.get("unsupportedConditions", []) or []),
        )


@dataclass
class Rule:
    name: str
    sequence: int | None = None
    enabled: bool = True
    exclude: bool = False
    conditions: Condition = field(default_factory=Condition)
    actions: Action = field(default_factory=Action)
    flags: Flags = field(default_factory=Flags)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sequence": self.sequence,
            "enabled": self.enabled,
            "exclude": self.exclude,
            "conditions": self.conditions.to_dict(),
            "actions": self.actions.to_dict(),
            "flags": self.flags.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Rule":
        return cls(
            name=d["name"],
            sequence=d.get("sequence"),
            enabled=bool(d.get("enabled", True)),
            exclude=bool(d.get("exclude", False)),
            conditions=Condition.from_dict(d.get("conditions")),
            actions=Action.from_dict(d.get("actions")),
            flags=Flags.from_dict(d.get("flags")),
        )


@dataclass
class RuleSet:
    rules: list[Rule] = field(default_factory=list)
    source_file: str | None = None
    source_mailbox: str | None = None
    extracted_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "source_mailbox": self.source_mailbox,
            "extracted_at": self.extracted_at or _now_iso(),
            "rules": [r.to_dict() for r in self.rules],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RuleSet":
        return cls(
            rules=[Rule.from_dict(r) for r in d.get("rules", [])],
            source_file=d.get("source_file"),
            source_mailbox=d.get("source_mailbox"),
            extracted_at=d.get("extracted_at"),
        )

    @classmethod
    def from_json(cls, text: str) -> "RuleSet":
        return cls.from_dict(json.loads(text))


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
