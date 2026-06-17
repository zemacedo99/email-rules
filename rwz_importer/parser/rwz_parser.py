"""Offline parser: Outlook Classic ``.rwz`` binary export -> ``RuleSet``.

A ``.rwz`` file is a proprietary MAPI blob. Strings are stored as UTF-16LE,
and senders are stored either as clean ``SMTP:`` addresses or as Exchange
X.500 EntryIDs (``/o=.../cn=Recipients/cn=<hex>-<localpart>``) bound to the
*source* mailbox. That EntryID binding is exactly why a ``.rwz`` cannot be
re-imported into a different mailbox, and why this tool rebuilds the rules
server-side instead.

The parser is anchor-based rather than offset-based, so it tolerates the
byte-alignment noise the format is full of. Three anchors carry the structure
(validated against the bundled fixture and locked by a regression baseline):

* ``14 00 <len>``                -> a rule's display name (one per rule block)
* ``<owner> 00 00 00 00 <len>``  -> the rule's destination folder name
* ``01 80 cd 00 00 00``          -> a subject-contains keyword list
* ``SMTP\\0`` (UTF-16LE)          -> a clean sender address

Anything that looks structural (X.500 paths, ``EX:`` prefixes, over-long blobs)
is dropped. The heuristics are documented inline where the format is non-obvious.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from rwz_importer.models import Action, Condition, Flags, Rule, RuleSet

# A pragmatic e-mail matcher. The trailing label may be a real TLD (``.com``)
# or a Microsoft service host whose last label is a word (``...@teams.mail.microsoft``).
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# UTF-16LE encoding of the literal "SMTP\0"; clean sender addresses follow it.
_SMTP_MARKER = bytes.fromhex("53004d00540050000000")

# Precedes a subject-contains list: <count:u32 LE> 00 00 00 00 <len> <kw> ...
_SUBJECT_MARKER = bytes.fromhex("0180cd000000")

# A title-cased multi-word label such as "Viva Engage".
_TITLE_RE = re.compile(r"^[A-Z][A-Za-z]+( [A-Z][A-Za-z]+)*$")


def parse_rwz_file(path: str | Path) -> RuleSet:
    """Parse a ``.rwz`` file from disk into a :class:`RuleSet`."""
    p = Path(path)
    return parse_rwz(p.read_bytes(), source_file=p.name)


def parse_rwz(data: bytes, source_file: str | None = None) -> RuleSet:
    """Parse raw ``.rwz`` bytes into a :class:`RuleSet` (pure, no network)."""
    owner = _detect_owner(data)
    owner_u = owner.encode("utf-16-le") if owner else b""

    anchors = _find_name_anchors(data)
    bounds = [off for off, _ in anchors] + [len(data)]

    rules: list[Rule] = []
    for idx, (start, raw_name) in enumerate(anchors):
        end = bounds[idx + 1]
        folder = _extract_folder(data, start, end, owner_u)
        froms = _extract_from_addresses(data, start, end, owner)
        subjects = _extract_subjects(data, start, end)

        # A name-anchor that holds an address or a quoted condition description
        # is not a user-given title; fall back to the destination folder name.
        name = raw_name
        if "@" in raw_name or "'" in raw_name or '"' in raw_name:
            name = folder or raw_name

        for label in _detect_secondary_labels(data, start, end, name, froms):
            name = f"{name} & {label}"

        rules.append(
            Rule(
                name=name,
                sequence=None,
                enabled=True,
                conditions=Condition(
                    fromAddresses=froms,
                    subjectContains=subjects,
                    senderContains=[],
                ),
                actions=Action(moveToFolder=folder, stopProcessingRules=True),
                flags=Flags(),
            )
        )

    rules = _merge_by_folder(rules)
    _assign_sequences(rules)
    _flag_cross_mailbox(rules, owner)

    return RuleSet(rules=rules, source_file=source_file, source_mailbox=owner or None)


# --------------------------------------------------------------------------- #
# Anchors & extraction helpers
# --------------------------------------------------------------------------- #


def _read_utf16(data: bytes, off: int, n_chars: int) -> str | None:
    """Decode ``n_chars`` UTF-16LE code units starting at ``off`` (or ``None``)."""
    raw = data[off : off + 2 * n_chars]
    if len(raw) < 2 * n_chars:
        return None
    try:
        return raw.decode("utf-16-le")
    except UnicodeDecodeError:
        return None


def _printable(s: str) -> bool:
    """True if every character is printable ASCII or a dash variant (en/em)."""
    return all(32 <= ord(c) < 127 or ord(c) in (0x2013, 0x2014) for c in s)


def _ascii_runs(data: bytes, lo: int, hi: int, min_len: int = 3) -> list[tuple[int, str]]:
    """Recover printable-ASCII UTF-16LE runs in ``[lo, hi)`` at both alignments."""
    runs: list[tuple[int, str]] = []
    for align in (lo, lo + 1):
        i = align
        cur: list[str] = []
        start: int | None = None
        while i + 1 < hi:
            b_lo, b_hi = data[i], data[i + 1]
            if b_hi == 0 and 32 <= b_lo < 127:
                if start is None:
                    start = i
                cur.append(chr(b_lo))
            else:
                if cur and len(cur) >= min_len:
                    runs.append((start, "".join(cur)))
                cur, start = [], None
            i += 2
        if cur and len(cur) >= min_len:
            runs.append((start, "".join(cur)))
    return runs


def _find_name_anchors(data: bytes) -> list[tuple[int, str]]:
    """Locate ``14 00 <len> <utf16 name>`` rule-name fields, in file order.

    Returns ``(string_offset, name)`` pairs. Each pair begins one rule block;
    a block runs until the next anchor (or end of file).
    """
    anchors: list[tuple[int, str]] = []
    i = 0
    n = len(data)
    while i < n - 3:
        if data[i] == 0x14 and data[i + 1] == 0x00:
            length = data[i + 2]
            if 1 <= length <= 60:
                s = _read_utf16(data, i + 3, length)
                if s and _printable(s) and s.strip() and not s.startswith(" "):
                    anchors.append((i + 3, s))
                    i += 3 + 2 * length
                    continue
        i += 1
    return anchors


def _detect_owner(data: bytes) -> str:
    """Infer the source-mailbox owner = the address most often used as a store marker.

    Every rule block ends with ``<owner> 00 00 00 00 <len> <folder>``. The owner
    address is therefore the e-mail whose UTF-16LE form is most frequently followed
    by that folder pattern. This is never emitted as a rule condition; it only
    drives cross-mailbox flagging.
    """
    counts: Counter[str] = Counter()
    for s in {m.group() for m in _EMAIL_RE.finditer(data.decode("latin1"))}:
        enc = s.encode("utf-16-le")
        for m in re.finditer(re.escape(enc), data):
            if _folder_after(data, m.end()) is not None:
                counts[s.lower()] += 1
    if counts:
        return counts.most_common(1)[0][0]
    # Fallback: most frequent address overall (single-byte ASCII view).
    allc = Counter(m.group().lower() for m in _EMAIL_RE.finditer(data.decode("latin1")))
    return allc.most_common(1)[0][0] if allc else ""


def _folder_after(data: bytes, owner_end: int) -> str | None:
    """Read the length-prefixed folder name following an owner-marker occurrence.

    Layout: ``<owner chars> | 00 00 (term) 00 00 (pad) | <len:u8> | <folder utf16>``.
    """
    p = owner_end
    zeros = 0
    while p < len(data) and data[p] == 0 and zeros < 6:
        p += 1
        zeros += 1
    if zeros < 2 or p >= len(data):
        return None
    length = data[p]
    if not 1 <= length <= 40:
        return None
    folder = _read_utf16(data, p + 1, length)
    if folder and folder.strip() and _printable(folder):
        return folder
    return None


def _extract_folder(data: bytes, start: int, end: int, owner_u: bytes) -> str | None:
    """Destination folder = name after the owner store-marker inside the block."""
    if not owner_u:
        return None
    found: str | None = None
    for m in re.finditer(re.escape(owner_u), data[start:end]):
        folder = _folder_after(data, start + m.end())
        if folder is not None:
            found = folder  # keep the last (folders sit at the block's tail)
    return found


def _clean_email(run: str) -> str | None:
    """Extract a clean address from a UTF-16 run, stripping a ``SMTP:`` prefix."""
    run = run.strip()
    if run[:5].upper() == "SMTP:":
        run = run[5:]
    if _EMAIL_RE.fullmatch(run):
        return run
    m = _EMAIL_RE.search(run)
    return m.group() if m else None


def _clean_x500_email(run: str) -> str | None:
    """Recover an address from the UTF-16 run that trails an X.500 EntryID.

    When a sender is stored only as an Exchange X.500 EntryID, the EntryID's last
    (single-byte) character bleeds into the following UTF-16 address as a glued-on
    capital, e.g. ``Khelpdesk@...`` -> ``helpdesk@...``. Strip that one capital when
    doing so still yields a valid address; otherwise treat the run as-is.
    """
    run = run.strip()
    if len(run) > 1 and run[0].isupper() and run[1].islower() and _EMAIL_RE.fullmatch(run[1:]):
        return run[1:]
    return _clean_email(run)


def _extract_from_addresses(data: bytes, start: int, end: int, owner: str) -> list[str]:
    """Sender addresses for the block: clean ``SMTP\\0`` copies, else X.500 fallback."""
    block = data[start:end]
    out: list[str] = []
    seen: set[str] = set()

    def add(addr: str | None) -> None:
        if not addr:
            return
        low = addr.lower()
        if owner and low == owner.lower():
            return
        if low not in seen:
            seen.add(low)
            out.append(addr)

    for m in re.finditer(re.escape(_SMTP_MARKER), block):
        end_off = m.end()
        chars: list[str] = []
        k = end_off
        while k + 1 < len(block):
            b_lo, b_hi = block[k], block[k + 1]
            if b_hi == 0 and 32 <= b_lo < 127:
                chars.append(chr(b_lo))
                k += 2
            else:
                break
        if chars:
            add(_clean_email("".join(chars)))

    if out:
        return out

    # No clean SMTP copy in this block: the sender is an X.500 EntryID. Recover
    # the address from the (junk-prefixed) UTF-16 run that carries it.
    for _, run in _ascii_runs(data, start, end):
        if "@" in run:
            addr = _clean_x500_email(run)
            if addr and (not owner or addr.lower() != owner.lower()):
                add(addr)
    return out


def _extract_subjects(data: bytes, start: int, end: int) -> list[str]:
    """Parse the subject-contains keyword list, if present in the block.

    Layout after the marker: ``<count:u32 LE> 00 00 00 00`` then ``count`` entries
    of ``<len:u8> <utf16 keyword>`` separated by null padding. Trailing whitespace
    on keywords (``"Accepted: "``) is trimmed.
    """
    block = data[start:end]
    m = re.search(re.escape(_SUBJECT_MARKER), block)
    if not m:
        return []
    p = m.end()
    if p + 4 > len(block):
        return []
    count = int.from_bytes(block[p : p + 4], "little")
    p += 4
    if not 1 <= count <= 64:
        return []
    out: list[str] = []
    for _ in range(count):
        while p < len(block) and block[p] == 0:
            p += 1
        if p >= len(block):
            break
        length = block[p]
        p += 1
        if not 1 <= length <= 128:
            break
        kw = _read_utf16(block, p, length)
        p += 2 * length
        if kw is None:
            break
        kw = kw.rstrip()
        if kw:
            out.append(kw)
    return out


def _detect_secondary_labels(
    data: bytes, start: int, end: int, name: str, froms: list[str]
) -> list[str]:
    """Find merged sub-rule labels inside a block (e.g. "Viva Engage").

    Some exports fold two same-destination rules into one block: a second sender
    address introduced by its own title-cased label. We surface that label so the
    merged rule keeps both names ("Weekly Digest & Viva Engage").
    """
    if len(froms) < 2:
        return []
    smtp_offsets = [m.end() for m in re.finditer(re.escape(_SMTP_MARKER), data[start:end])]
    if not smtp_offsets:
        return []
    region_lo = start + min(smtp_offsets)
    region_hi = start + max(smtp_offsets) + 200
    labels: list[str] = []
    seen: set[str] = set()
    for off, run in _ascii_runs(data, start, end):
        val = run.strip()
        low = val.lower()
        if not region_lo <= off <= region_hi:
            continue
        if "@" in val or len(val) < 5:
            continue
        if low == name.strip().lower() or low in seen:
            continue
        if _TITLE_RE.match(val):
            seen.add(low)
            labels.append(val)
    return labels


# --------------------------------------------------------------------------- #
# Post-processing
# --------------------------------------------------------------------------- #


def _merge_by_folder(rules: list[Rule]) -> list[Rule]:
    """Collapse rules that target the same folder with the same action into one OR-rule."""
    merged: list[Rule] = []
    index: dict[tuple, int] = {}
    for rule in rules:
        key = (
            (rule.actions.moveToFolder or "").strip().lower(),
            rule.actions.stopProcessingRules,
        )
        if not key[0]:
            merged.append(rule)
            continue
        if key in index:
            target = merged[index[key]]
            for addr in rule.conditions.fromAddresses:
                if addr.lower() not in {a.lower() for a in target.conditions.fromAddresses}:
                    target.conditions.fromAddresses.append(addr)
            for kw in rule.conditions.subjectContains:
                if kw not in target.conditions.subjectContains:
                    target.conditions.subjectContains.append(kw)
            if rule.name not in target.name.split(" & "):
                target.name = f"{target.name} & {rule.name}"
        else:
            index[key] = len(merged)
            merged.append(rule)
    return merged


def _assign_sequences(rules: list[Rule]) -> None:
    """Number rules 1..N in extraction order (Exchange requires Sequence >= 1)."""
    for i, rule in enumerate(rules, start=1):
        rule.sequence = i


def _flag_cross_mailbox(rules: list[Rule], owner: str) -> None:
    """Flag sender addresses that look like the owner on a *different* domain.

    These are almost always stale references to the source mailbox and must be
    reviewed (never silently rewritten) before applying to the target mailbox.
    """
    if not owner or "@" not in owner:
        return
    owner_local, owner_domain = owner.lower().split("@", 1)
    for rule in rules:
        for addr in rule.conditions.fromAddresses:
            if "@" not in addr:
                continue
            local, domain = addr.lower().split("@", 1)
            if local == owner_local and domain != owner_domain:
                rule.flags.addressMayBelongToDifferentMailbox = True
