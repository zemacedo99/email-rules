"""Generic IMAP backend: folders + messages over IMAP, rules as a Sieve script.

Conforms to :class:`~rwz_importer.api.provider.MailboxProvider` for any server that
speaks IMAP (folders, message metadata, moves) and **ManageSieve** (server-side rules
as a Sieve script). That covers Dovecot/Cyrus/Fastmail and similar; it does *not* cover
Gmail/Outlook (which expose no ManageSieve). Auth is host/username/password (e.g. an app
password), not OAuth.

Rules live in a single active Sieve script. We use ``sievelib``'s ``FiltersSet`` to both
generate and parse it, so a rule round-trips through the script. The whole script is
loaded, edited, and written back — preserving any other filters ``sievelib`` can parse;
if the existing script can't be parsed we refuse to overwrite it (so a hand-written
script is never clobbered). Capabilities match Exchange (ordered + stop-processing +
folder destinations), so the engine's sequencing/shadow logic is reused as-is.

IMAP/ManageSieve access is injectable (``imap`` / ``sieve_store``) so the logic is
unit-tested without a network; connections are made lazily on first use otherwise.
"""

from __future__ import annotations

import imaplib
import io
import logging
import re
from typing import Callable

from sievelib.factory import FiltersSet
from sievelib.parser import Parser

from rwz_importer.api.capabilities import IMAP_CAPABILITIES, ProviderCapabilities
from rwz_importer.api.errors import MailboxApiError
from rwz_importer.api.owa_client import ExistingRule, Folder, Message

logger = logging.getLogger("rwz_importer.api")

_SCRIPT_NAME = "rwz-importer"
_INBOX = "INBOX"

# IMAP semantics align with Exchange (ordered rules, stop-processing, folder
# destinations). The canonical value lives in api.capabilities so the registry/UI can
# read it without importing this module (which pulls in the optional ``sievelib`` dep).
CAPABILITIES = IMAP_CAPABILITIES


class ImapSieveError(MailboxApiError):
    """An IMAP or ManageSieve operation failed."""


class SieveStore:
    """Reads/writes the active Sieve script over ManageSieve (via ``sievelib``)."""

    def __init__(self, host: str, port: int, username: str, password: str, *, starttls: bool = True):
        self._host, self._port = host, port
        self._user, self._password = username, password
        self._starttls = starttls

    def _client(self):
        from sievelib.managesieve import Client

        client = Client(self._host, self._port)
        if not client.connect(self._user, self._password, starttls=self._starttls):
            raise ImapSieveError(
                f"ManageSieve login to {self._host}:{self._port} failed (check host/credentials)."
            )
        return client

    def read(self) -> str:
        """Return the active script's text (``""`` if there is no active script)."""
        client = self._client()
        try:
            listing = client.listscripts()
            active = listing[0] if listing else None
            if not active:
                return ""
            return client.getscript(active) or ""
        finally:
            client.logout()

    def write(self, text: str) -> None:
        """Replace the managed script and make it active."""
        client = self._client()
        try:
            if not client.putscript(_SCRIPT_NAME, text):
                raise ImapSieveError("ManageSieve putscript failed (script rejected by server).")
            client.setactive(_SCRIPT_NAME)
        finally:
            client.logout()


class ImapSieveClient:
    """IMAP folders/messages/moves + Sieve rules for a generic IMAP server."""

    def __init__(
        self,
        host: str,
        *,
        port: int = 993,
        username: str = "",
        password: str = "",
        use_ssl: bool = True,
        sieve_host: str | None = None,
        sieve_port: int = 4190,
        sieve_starttls: bool = True,
        imap: imaplib.IMAP4 | None = None,
        sieve_store: SieveStore | None = None,
    ):
        if not host and imap is None:
            raise ValueError("An IMAP host is required.")
        self._host, self._port, self._use_ssl = host, port, use_ssl
        self._username, self._password = username, password
        self._sieve_host = sieve_host or host
        self._sieve_port, self._sieve_starttls = sieve_port, sieve_starttls
        self._imap_conn = imap
        self._store_obj = sieve_store
        self._selected: str | None = None

    def __enter__(self) -> "ImapSieveClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._imap_conn is not None:
            try:
                self._imap_conn.logout()
            except Exception:  # pragma: no cover - best effort
                pass
            self._imap_conn = None

    # --------------------------------------------------------- Capabilities --
    def capabilities(self) -> ProviderCapabilities:
        return CAPABILITIES

    # ------------------------------------------------------------ plumbing --
    def _imap(self) -> imaplib.IMAP4:
        if self._imap_conn is None:
            try:
                conn = imaplib.IMAP4_SSL(self._host, self._port) if self._use_ssl else imaplib.IMAP4(self._host, self._port)
                conn.login(self._username, self._password)
            except Exception as exc:  # imaplib raises imaplib.IMAP4.error and socket errors
                raise ImapSieveError(f"IMAP login to {self._host} failed: {exc}") from exc
            self._imap_conn = conn
        return self._imap_conn

    def _store(self) -> SieveStore:
        if self._store_obj is None:
            self._store_obj = SieveStore(
                self._sieve_host, self._sieve_port, self._username, self._password,
                starttls=self._sieve_starttls,
            )
        return self._store_obj

    def _select(self, mailbox: str) -> None:
        if self._selected != mailbox:
            self._check(self._imap().select(mailbox, readonly=True), f"select {mailbox}")
            self._selected = mailbox

    @staticmethod
    def _check(result, what: str):
        typ, data = result
        if typ != "OK":
            raise ImapSieveError(f"IMAP {what} failed: {data!r}")
        return data

    # -------------------------------------------------------------- Folders --
    def list_all_folders(self) -> list[Folder]:
        data = self._check(self._imap().list(), "list")
        folders: list[Folder] = []
        for line in data or []:
            name = _parse_list_mailbox(line)
            if name and name.upper() != _INBOX:  # the inbox itself is not a destination
                folders.append(Folder(id=name, display_name=name))
        return folders

    def list_inbox_folders(self) -> list[Folder]:
        # IMAP LIST already returns the full (flat) tree, so there's nothing extra here.
        return []

    def list_child_folders(self, parent_id: str) -> list[Folder]:
        # list_all_folders already enumerates the whole hierarchy.
        return []

    def create_folder(self, display_name: str) -> Folder:
        self._check(self._imap().create(display_name), f"create {display_name}")
        self._selected = None
        return Folder(id=display_name, display_name=display_name)

    # ---------------------------------------------------------------- Rules --
    def _load(self) -> FiltersSet:
        text = self._store().read()
        fs = FiltersSet(_SCRIPT_NAME)
        if text.strip():
            parser = Parser()
            if not parser.parse(text):
                raise ImapSieveError(
                    "The existing Sieve script could not be parsed; refusing to overwrite it. "
                    "Remove or simplify it, or manage rules by hand."
                )
            fs.from_parser_result(parser)
        return fs

    def _save(self, fs: FiltersSet) -> None:
        buf = io.StringIO()
        fs.tosieve(buf)
        self._store().write(buf.getvalue())

    def list_rules(self) -> list[ExistingRule]:
        fs = self._load()
        return [
            self._existing_rule(fs, entry["name"], index + 1, enabled=bool(entry.get("enabled", True)))
            for index, entry in enumerate(fs.filters)
        ]

    def _existing_rule(self, fs: FiltersSet, name: str, sequence: int, *, enabled: bool = True) -> ExistingRule:
        conditions = fs.get_filter_conditions(name) or []
        actions = fs.get_filter_actions(name) or []
        # sievelib returns values verbatim (escapes not removed), so unescape on read.
        from_terms = [_sieve_unescape(v) for (header, _mt, v) in conditions if header.lower() == "from"]
        subject_terms = [_sieve_unescape(v) for (header, _mt, v) in conditions if header.lower() == "subject"]
        return ExistingRule(
            id=name,
            display_name=name,
            sequence=sequence,
            is_enabled=enabled,
            from_addresses=from_terms,
            subject_contains=subject_terms,
            sender_contains=[],  # Sieve header tests don't distinguish exact vs contains
            move_to_folder_id=_fileinto_folder(actions),
            stop_processing=any(a and a[0] == "stop" for a in actions),
        )

    def create_rule(
        self,
        *,
        display_name: str,
        sequence: int,
        folder_id: str | None,
        from_addresses: list[str] | None = None,
        subject_contains: list[str] | None = None,
        sender_contains: list[str] | None = None,
        stop_processing: bool = True,
        enabled: bool = True,
        archive: bool = True,
    ) -> ExistingRule:
        """Add a rule to the Sieve script. ``archive`` is ignored (fileinto already moves)."""
        senders = [v.strip() for v in [*(from_addresses or []), *(sender_contains or [])] if v and v.strip()]
        subjects = [v.strip() for v in (subject_contains or []) if v and v.strip()]
        if not senders and not subjects:
            raise ValueError("A Sieve rule needs at least one sender or subject condition.")
        # Semantics: OR within a header type, AND across types. sievelib's flat matchtype can't
        # nest anyof inside allof, so a rule that mixes senders AND subjects where one of them has
        # several values would compile to a contradictory allof that matches nothing. Refuse it
        # (clear error) rather than silently dropping the rule's mail. Split into one rule per type.
        if senders and subjects and (len(senders) > 1 or len(subjects) > 1):
            raise ValueError(
                "This rule mixes senders and subjects where one has several values. Sieve can't "
                "express OR-within-a-type combined with AND-across-types as a single filter here; "
                "split it into separate rules (one per condition type)."
            )
        conditions = [("from", ":contains", _sieve_escape(v)) for v in senders]
        conditions += [("Subject", ":contains", _sieve_escape(v)) for v in subjects]
        matchtype = "allof" if (senders and subjects) else "anyof"

        actions: list[tuple] = []
        if folder_id:
            actions.append(("fileinto", _sieve_escape(folder_id)))
        if stop_processing:
            actions.append(("stop",))
        if not actions:
            actions.append(("keep",))

        fs = self._load()
        index = None
        if fs.filter_exists(display_name):  # single-script model: create is upsert-by-name
            index = next((i for i, e in enumerate(fs.filters) if e["name"] == display_name), None)
            fs.removefilter(display_name)
        fs.addfilter(display_name, conditions, actions, matchtype=matchtype)
        if index is not None and fs.filters:
            fs.filters.insert(index, fs.filters.pop())  # keep the rule's original precedence
        self._save(fs)
        seq = next((i + 1 for i, e in enumerate(fs.filters) if e["name"] == display_name), len(fs.filters))
        return self._existing_rule(fs, display_name, seq)

    def delete_rule(self, rule_id: str) -> None:
        if not rule_id:
            raise ValueError("A rule name is required to delete a Sieve rule.")
        fs = self._load()
        if fs.filter_exists(rule_id):
            fs.removefilter(rule_id)
            self._save(fs)

    # ------------------------------------------------------------- Messages --
    def list_messages(self, *, folder: str = "Inbox", top: int = 200) -> list[Message]:
        mailbox = _INBOX if folder.strip().lower() in ("inbox", "") else folder
        self._select(mailbox)
        data = self._check(self._imap().uid("SEARCH", None, "ALL"), "search")
        uids = (data[0].split() if data and data[0] else [])
        uids = uids[-max(1, top):][::-1]  # newest first
        messages: list[Message] = []
        for uid in uids:
            fetched = self._check(
                self._imap().uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"),
                "fetch",
            )
            headers = _parse_fetch_headers(fetched)
            name, address = _parse_from(headers.get("from", ""))
            messages.append(
                Message(
                    id=uid.decode() if isinstance(uid, bytes) else str(uid),
                    subject=(headers.get("subject") or "").strip(),
                    sender_address=address,
                    sender_name=name,
                    received=headers.get("date"),
                )
            )
        return messages

    def move_message(self, message_id: str, destination_folder_id: str) -> str:
        if not message_id:
            raise ValueError("A message id (UID) is required to move a message.")
        if not destination_folder_id:
            raise ValueError("A destination folder is required to move a message.")
        conn = self._imap()
        self._check(conn.select(_INBOX), "select INBOX")
        self._selected = _INBOX
        uid = message_id.encode() if isinstance(message_id, str) else message_id
        # Prefer RFC 6851 MOVE; fall back to COPY + \Deleted + EXPUNGE. A server without MOVE
        # may answer BAD, which imaplib raises as IMAP4.error (not our ImapSieveError) — catch
        # both (and OSError) so the fallback actually runs on those servers.
        try:
            self._check(conn.uid("MOVE", uid, destination_folder_id), "move")
        except (ImapSieveError, imaplib.IMAP4.error, OSError):
            self._check(conn.uid("COPY", uid, destination_folder_id), "copy")
            self._check(conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)"), "store")
            conn.expunge()
        return message_id


# --------------------------------------------------------------------- helpers --
_LIST_RE = re.compile(rb'^\([^)]*\)\s+(?:"[^"]*"|NIL)\s+(?P<name>"(?:[^"\\]|\\.)*"|[^\s]+)\s*$')


def _parse_list_mailbox(line) -> str | None:
    """Pull the mailbox name out of one IMAP ``LIST`` response line.

    A name returned as an IMAP literal arrives as a ``(prefix_with_{n}, name_bytes)`` tuple;
    the trailing bytes are the name itself, so use them directly rather than regex-matching.
    """
    if isinstance(line, tuple):
        name = line[-1]
        if isinstance(name, (bytes, bytearray)):
            return bytes(name).decode("utf-8", "replace") or None
        return None
    if not isinstance(line, bytes):
        line = str(line).encode()
    match = _LIST_RE.match(line.strip())
    if not match:
        return None
    raw = match.group("name").decode("utf-8", "replace")
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return raw


def _parse_fetch_headers(fetched) -> dict[str, str]:
    """Parse the header bytes returned by a FETCH HEADER.FIELDS into a dict."""
    blob = b""
    for part in fetched or []:
        if isinstance(part, tuple) and len(part) > 1:
            blob += part[1] if isinstance(part[1], bytes) else str(part[1]).encode()
    import email

    msg = email.message_from_bytes(blob)
    return {k.lower(): str(v) for k, v in msg.items()}


def _parse_from(value: str) -> tuple[str, str]:
    """Split a ``"Name <addr>"`` (or bare address) From header into (name, address)."""
    value = (value or "").strip()
    if "<" in value and ">" in value:
        name = value[: value.index("<")].strip().strip('"').strip()
        address = value[value.index("<") + 1 : value.index(">")].strip()
        return name, address.lower()
    return "", value.lower()


def _sieve_escape(value: str) -> str:
    """Escape a value for a Sieve quoted-string (RFC 5228): backslash then double-quote.

    sievelib does not escape values it writes, so an unescaped ``"`` or ``\\`` would produce a
    syntactically invalid script — we escape before handing values to it.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _sieve_unescape(value: str) -> str:
    """Reverse :func:`_sieve_escape` (sievelib returns the raw escaped inner text on read)."""
    return re.sub(r"\\(.)", r"\1", value)


def _fileinto_folder(actions) -> str | None:
    """Destination of a ``fileinto`` action: its last string arg, skipping tags like ``:copy``."""
    for action in actions:
        if action and action[0] == "fileinto":
            for arg in reversed(action[1:]):
                if isinstance(arg, str) and not arg.startswith(":"):
                    return _sieve_unescape(arg)
    return None
