"""IMAP/Sieve client tests with in-memory fakes (no IMAP or ManageSieve server).

The rule logic round-trips through a real Sieve script (built and parsed by sievelib);
folders/messages/move are exercised against a small fake that mimics imaplib's return
shapes. Skipped entirely if the optional ``sievelib`` dependency isn't installed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("sievelib")

from rwz_importer.api.imap_sieve_client import ImapSieveClient  # noqa: E402
from rwz_importer.api.provider import MailboxProvider  # noqa: E402


class FakeStore:
    """In-memory stand-in for the ManageSieve active-script store."""

    def __init__(self, text: str = "") -> None:
        self.text = text

    def read(self) -> str:
        return self.text

    def write(self, text: str) -> None:
        self.text = text


class FakeIMAP:
    """Minimal imaplib-shaped fake: returns (typ, data) tuples for the calls we make."""

    def __init__(self, mailboxes=None, search=b"", fetch_headers=None, move_raises=False) -> None:
        self._mailboxes = mailboxes or [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren) "/" "Work"',
            b'(\\HasChildren) "/" Archive',
        ]
        self._search = search
        self._fetch_headers = fetch_headers or {}
        self._move_raises = move_raises  # simulate a server without RFC 6851 MOVE
        self.created: list[str] = []
        self.moved: list[tuple] = []

    def list(self, *a, **k):
        return "OK", list(self._mailboxes)

    def create(self, name):
        self.created.append(name)
        return "OK", [b"created"]

    def select(self, mailbox, readonly=False):
        return "OK", [b"3"]

    def uid(self, command, *args):
        command = command.upper()
        if command == "SEARCH":
            return "OK", [self._search]
        if command == "FETCH":
            uid = args[0]
            key = uid.decode() if isinstance(uid, bytes) else str(uid)
            blob = self._fetch_headers.get(key, b"")
            return "OK", [(b"%s (UID %s ...)" % (key.encode(), key.encode()), blob), b")"]
        if command == "MOVE" and self._move_raises:
            import imaplib
            raise imaplib.IMAP4.error("BAD UID MOVE not supported")
        if command in ("MOVE", "COPY", "STORE"):
            self.moved.append((command, args))
            return "OK", [b"done"]
        return "OK", [b""]

    def expunge(self):
        return "OK", [b""]

    def logout(self):
        return "OK", [b"bye"]


def _client(store=None, imap=None) -> ImapSieveClient:
    return ImapSieveClient(
        "imap.example.com",
        username="me@example.com",
        password="pw",
        imap=imap or FakeIMAP(),
        sieve_store=store if store is not None else FakeStore(),
    )


def test_capabilities_match_exchange_baseline() -> None:
    caps = _client().capabilities()
    assert caps.ordered_rules and caps.stop_processing
    assert caps.destination_kind == "folder"
    assert caps.archive_toggle is False
    assert caps.hierarchical_folders is True


def test_create_rule_round_trips_through_sieve() -> None:
    store = FakeStore()
    client = _client(store=store)
    client.create_rule(
        display_name="WorkMail", sequence=1, folder_id="Work",
        from_addresses=["boss@x.com"], subject_contains=["Urgent"], stop_processing=True,
    )
    # A real Sieve script was written.
    assert "fileinto" in store.text and "Work" in store.text and "stop;" in store.text

    rules = client.list_rules()
    assert len(rules) == 1
    r = rules[0]
    assert r.id == "WorkMail" and r.display_name == "WorkMail"
    assert r.from_addresses == ["boss@x.com"]
    assert r.subject_contains == ["Urgent"]
    assert r.move_to_folder_id == "Work"
    assert r.stop_processing is True
    assert r.sequence == 1


def test_multi_value_single_type_uses_or() -> None:
    store = FakeStore()
    client = _client(store=store)
    client.create_rule(
        display_name="Senders", sequence=1, folder_id="Work",
        from_addresses=["a@x.com", "b@y.com"],
    )
    assert "anyof" in store.text  # OR across the two senders
    rule = client.list_rules()[0]
    assert set(rule.from_addresses) == {"a@x.com", "b@y.com"}


def test_create_rule_requires_a_condition() -> None:
    client = _client()
    with pytest.raises(ValueError):
        client.create_rule(display_name="Empty", sequence=1, folder_id="Work")


def test_delete_rule_removes_it() -> None:
    store = FakeStore()
    client = _client(store=store)
    client.create_rule(display_name="R1", sequence=1, folder_id="Work", from_addresses=["a@x.com"])
    client.create_rule(display_name="R2", sequence=2, folder_id="Spam", from_addresses=["b@y.com"])
    assert {r.id for r in client.list_rules()} == {"R1", "R2"}
    client.delete_rule("R1")
    assert {r.id for r in client.list_rules()} == {"R2"}


def test_create_preserves_foreign_filters() -> None:
    # A pre-existing, sievelib-parseable filter must survive adding ours.
    seed = (
        'require ["fileinto"];\n\n'
        '# Filter: Newsletter\n'
        'if anyof (header :contains "from" "news@site.com") {\n'
        '    fileinto "News";\n}\n'
    )
    store = FakeStore(seed)
    client = _client(store=store)
    client.create_rule(display_name="Mine", sequence=1, folder_id="Work", from_addresses=["a@x.com"])
    assert {r.id for r in client.list_rules()} == {"Newsletter", "Mine"}


def test_refuses_to_overwrite_unparseable_script() -> None:
    store = FakeStore("this is { not valid sieve @@@")
    client = _client(store=store)
    from rwz_importer.api.imap_sieve_client import ImapSieveError

    with pytest.raises(ImapSieveError):
        client.list_rules()


def test_list_folders_parses_imap_list_and_excludes_inbox() -> None:
    client = _client(imap=FakeIMAP())
    names = {f.display_name for f in client.list_all_folders()}
    assert names == {"Work", "Archive"}  # INBOX itself excluded as a destination
    assert all(f.id == f.display_name for f in client.list_all_folders())  # id == path


def test_list_folders_handles_literal_mailbox_names() -> None:
    """A mailbox name returned as an IMAP literal (a tuple) must not be dropped."""
    imap = FakeIMAP(mailboxes=[
        b'(\\HasNoChildren) "/" "Work"',
        (b'(\\HasNoChildren) "/" {7}', b"Rep\xc3\xb8rts"),  # literal (non-ASCII) name
    ])
    names = {f.display_name for f in _client(imap=imap).list_all_folders()}
    assert "Work" in names and "Repørts" in names


def test_create_folder_issues_imap_create() -> None:
    imap = FakeIMAP()
    client = _client(imap=imap)
    folder = client.create_folder("Projects")
    assert folder.id == "Projects" and "Projects" in imap.created


def test_list_messages_parses_metadata() -> None:
    imap = FakeIMAP(
        search=b"1 2",
        fetch_headers={
            "2": b"From: Bob <bob@x.com>\r\nSubject: Hi there\r\nDate: 2026-01-02\r\n\r\n",
            "1": b"From: a@b.com\r\nSubject: Older\r\n\r\n",
        },
    )
    client = _client(imap=imap)
    messages = client.list_messages(top=10)
    # Newest first (UID 2 before 1).
    assert messages[0].sender_address == "bob@x.com" and messages[0].sender_name == "Bob"
    assert messages[0].subject == "Hi there"
    assert messages[1].sender_address == "a@b.com"


def test_move_message_prefers_move_extension() -> None:
    imap = FakeIMAP()
    client = _client(imap=imap)
    out = client.move_message("7", "Work")
    assert out == "7"
    assert imap.moved and imap.moved[0][0] == "MOVE"


def test_multi_type_multi_value_rule_is_refused() -> None:
    """Several senders AND a subject can't be one correct Sieve filter — refuse, don't silently drop."""
    client = _client()
    with pytest.raises(ValueError):
        client.create_rule(
            display_name="Mixed", sequence=1, folder_id="Work",
            from_addresses=["a@x.com", "b@y.com"], subject_contains=["Invoice"],
        )
    # But one-of-each (1 sender + 1 subject) is fine (allof).
    client.create_rule(
        display_name="OneEach", sequence=1, folder_id="Work",
        from_addresses=["a@x.com"], subject_contains=["Invoice"],
    )


def test_condition_values_are_escaped_and_round_trip() -> None:
    """A value with a double-quote must produce a VALID script and read back unchanged."""
    from sievelib.parser import Parser

    store = FakeStore()
    client = _client(store=store)
    client.create_rule(
        display_name="Quoted", sequence=1, folder_id='Wo"rk',
        subject_contains=['he said "hi" today'],
    )
    assert Parser().parse(store.text) is True  # the written script is syntactically valid
    rule = client.list_rules()[0]
    assert rule.subject_contains == ['he said "hi" today']  # unescaped on read
    assert rule.move_to_folder_id == 'Wo"rk'


def test_move_falls_back_when_move_extension_unsupported() -> None:
    imap = FakeIMAP(move_raises=True)
    client = _client(imap=imap)
    out = client.move_message("7", "Work")
    assert out == "7"
    kinds = [m[0] for m in imap.moved]
    assert "COPY" in kinds and "STORE" in kinds  # fell back since MOVE raised IMAP4.error


def test_disabled_foreign_filter_reported_disabled() -> None:
    import io
    from sievelib.factory import FiltersSet

    fs = FiltersSet("seed")
    fs.addfilter("Off", [("from", ":contains", "x@y.com")], [("fileinto", "F")])
    fs.disablefilter("Off")
    buf = io.StringIO(); fs.tosieve(buf)
    client = _client(store=FakeStore(buf.getvalue()))
    rule = next(r for r in client.list_rules() if r.id == "Off")
    assert rule.is_enabled is False


def test_foreign_fileinto_copy_folder_is_read() -> None:
    seed = (
        'require ["fileinto", "copy"];\n\n'
        '# Filter: Keep\n'
        'if anyof (header :contains "from" "x@y.com") {\n'
        '    fileinto :copy "Saved";\n}\n'
    )
    client = _client(store=FakeStore(seed))
    rule = client.list_rules()[0]
    assert rule.move_to_folder_id == "Saved"  # not ":copy"


def test_upsert_preserves_rule_position() -> None:
    store = FakeStore()
    client = _client(store=store)
    client.create_rule(display_name="R1", sequence=1, folder_id="A", from_addresses=["a@x.com"])
    client.create_rule(display_name="R2", sequence=2, folder_id="B", from_addresses=["b@y.com"])
    # Re-create R1 (an edit) with a new folder — it must stay first, not jump to the end.
    client.create_rule(display_name="R1", sequence=1, folder_id="C", from_addresses=["a@x.com"])
    rules = client.list_rules()
    assert [r.id for r in rules] == ["R1", "R2"]
    assert rules[0].move_to_folder_id == "C"


def test_imapsieveclient_satisfies_provider() -> None:
    assert isinstance(_client(), MailboxProvider)
