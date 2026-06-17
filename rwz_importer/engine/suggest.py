"""Suggest new inbox rules by spotting filing patterns in recent mail.

Read-only and offline: given a sample of message *metadata* (never bodies) plus
the rules already in place, it proposes additional rules that would file traffic
nothing currently catches. Three signals are mined:

* **high-volume senders** — one address sending a lot of unfiled mail;
* **domain clusters** — many senders at one domain (e.g. a vendor);
* **subject tags** — bracketed markers like ``[JIRA]`` that recur.

Every :class:`RuleSuggestion` converts to a :class:`~rwz_importer.models.Rule` via
:meth:`RuleSuggestion.to_rule`, so an accepted suggestion drops straight into the
existing plan -> apply pipeline. Suggestions are advisory; nothing is applied here.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from rwz_importer.api.owa_client import ExistingRule, Message
from rwz_importer.models import Action, Condition, Flags, Rule

_DOMAIN_RE = re.compile(r"@([A-Za-z0-9.\-]+)$")
_TAG_RE = re.compile(r"\[([^\]]{2,30})\]")
_EXAMPLES = 3


@dataclass
class RuleSuggestion:
    """A proposed rule, with the evidence behind it. Not yet applied."""

    name: str
    reason: str
    message_count: int
    condition: Condition
    suggested_folder: str
    examples: list[str] = field(default_factory=list)
    confidence: float = 0.0

    def to_rule(self) -> Rule:
        """Convert into a canonical :class:`Rule` ready for the apply pipeline."""
        return Rule(
            name=self.name,
            conditions=self.condition,
            actions=Action(moveToFolder=self.suggested_folder, stopProcessingRules=True),
            flags=Flags(),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "reason": self.reason,
            "messageCount": self.message_count,
            "confidence": round(self.confidence, 2),
            "suggestedFolder": self.suggested_folder,
            "conditions": self.condition.to_dict(),
            "examples": list(self.examples),
        }


def suggest_rules(
    messages: list[Message],
    existing_rules: list[ExistingRule],
    *,
    min_sender: int = 3,
    min_domain: int = 6,
    min_subject: int = 4,
    max_suggestions: int = 20,
) -> list[RuleSuggestion]:
    """Mine ``messages`` for rules that aren't already covered by ``existing_rules``.

    Thresholds are the minimum message counts for a sender / domain / subject-tag
    suggestion to surface. Results are ranked by message count, most first.
    """
    covered_senders = _covered_senders(existing_rules)
    sender_needles = _needles(existing_rules, "sender_contains")
    subject_needles = _needles(existing_rules, "subject_contains")

    by_sender: Counter[str] = Counter()
    by_domain: Counter[str] = Counter()
    domain_senders: dict[str, set[str]] = defaultdict(set)
    by_tag: Counter[str] = Counter()
    tag_display: dict[str, str] = {}

    sender_examples: dict[str, list[str]] = defaultdict(list)
    domain_examples: dict[str, list[str]] = defaultdict(list)
    tag_examples: dict[str, list[str]] = defaultdict(list)

    total = 0
    for message in messages:
        addr = message.sender_address.strip().lower()
        subject = message.subject.strip()
        if addr:
            total += 1
            by_sender[addr] += 1
            _add_example(sender_examples[addr], subject)
            match = _DOMAIN_RE.search(addr)
            if match:
                domain = match.group(1)
                by_domain[domain] += 1
                domain_senders[domain].add(addr)
                _add_example(domain_examples[domain], subject)
        for tag in _TAG_RE.findall(subject):
            key = tag.strip().lower()
            by_tag[key] += 1
            tag_display.setdefault(key, tag.strip())
            _add_example(tag_examples[key], subject)

    suggestions: list[RuleSuggestion] = []

    for addr, count in by_sender.items():
        if count < min_sender or _sender_covered(addr, covered_senders, sender_needles):
            continue
        suggestions.append(
            RuleSuggestion(
                name=f"From {addr}",
                reason=f"{count} recent messages from {addr} aren't filed by any rule.",
                message_count=count,
                condition=Condition(fromAddresses=[addr]),
                suggested_folder=_folder_from_domain(addr),
                examples=list(sender_examples[addr]),
                confidence=_confidence(count, total),
            )
        )

    for domain, count in by_domain.items():
        # Need several distinct senders, else the per-sender rule already covers it.
        if count < min_domain or len(domain_senders[domain]) < 2:
            continue
        needle = f"@{domain}"
        if _needle_covered(needle, sender_needles):
            continue
        suggestions.append(
            RuleSuggestion(
                name=f"Domain {domain}",
                reason=(
                    f"{count} messages from {len(domain_senders[domain])} senders at "
                    f"{domain} aren't filed by any rule."
                ),
                message_count=count,
                condition=Condition(senderContains=[needle]),
                suggested_folder=_folder_name(domain),
                examples=list(domain_examples[domain]),
                confidence=_confidence(count, total),
            )
        )

    for key, count in by_tag.items():
        if count < min_subject or _needle_covered(key, subject_needles):
            continue
        display = tag_display[key]
        suggestions.append(
            RuleSuggestion(
                name=f"Subject tag [{display}]",
                reason=f"{count} messages share the subject tag '{display}'.",
                message_count=count,
                condition=Condition(subjectContains=[display]),
                suggested_folder=_folder_name(display),
                examples=list(tag_examples[key]),
                confidence=_confidence(count, total),
            )
        )

    suggestions.sort(key=lambda s: (s.message_count, s.confidence), reverse=True)
    return suggestions[:max_suggestions]


# --------------------------------------------------------------------- helpers --
def _add_example(bucket: list[str], subject: str) -> None:
    if subject and len(bucket) < _EXAMPLES and subject not in bucket:
        bucket.append(subject)


def _covered_senders(rules: list[ExistingRule]) -> set[str]:
    return {a.strip().lower() for r in rules for a in r.from_addresses if a}


def _needles(rules: list[ExistingRule], attr: str) -> list[str]:
    return [n.strip().lower() for r in rules for n in getattr(r, attr) if n]


def _sender_covered(addr: str, covered: set[str], needles: list[str]) -> bool:
    return addr in covered or any(n in addr for n in needles)


def _needle_covered(needle: str, needles: list[str]) -> bool:
    return any(needle in n or n in needle for n in needles)


def _confidence(count: int, total: int) -> float:
    """Rough 0..1 score: share of sampled mail this pattern represents, scaled up."""
    if total <= 0:
        return 0.0
    return min(1.0, (count / total) * 3)


def _folder_from_domain(addr: str) -> str:
    match = _DOMAIN_RE.search(addr)
    return _folder_name(match.group(1)) if match else "Filed"


def _folder_name(raw: str) -> str:
    """A human-ish folder name from a domain or tag (a starting point to rename)."""
    core = raw.split(".")[0] if "." in raw else raw
    pretty = core.replace("-", " ").replace("_", " ").strip()
    return pretty.title() if pretty else "Filed"
