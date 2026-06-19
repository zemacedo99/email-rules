"""Gmail backend: labels as folders, filters as rules, via the Gmail REST API.

Targets ``https://gmail.googleapis.com/gmail/v1/users/me/`` with an OAuth 2.0
Bearer token (scopes ``gmail.settings.basic``, ``gmail.labels``, ``gmail.modify``).
It conforms to :class:`~rwz_importer.api.provider.MailboxProvider`, so the service,
CLI, MCP server, and web UI drive it through exactly the same surface as the OWA
client — but Gmail's model differs in ways the capability flags encode:

* **Labels, not folders.** A "folder" is a Gmail label; the ``/`` in a label name
  is display-only nesting, so :meth:`capabilities` reports ``hierarchical_folders``
  False and there is no per-folder child listing.
* **Filters, not ordered rules.** Filters are unordered and have no stop-processing
  and no name, so ``ordered_rules``/``stop_processing`` are False and a display name
  is synthesised from the criteria for the UI.
* **Move = label (+ archive).** "Move to X" applies label X; the per-rule ``archive``
  choice additionally removes the ``INBOX`` label (``archive_toggle`` True).

Conditions map to a Gmail search ``query`` (not the structured ``from``/``subject``
criteria fields, which are plain phrase-matches with no boolean support). Sender terms
(exact addresses and ``contains`` fragments alike — Gmail can't distinguish them) become
a ``from:(a OR b)`` group; subject fragments become a ``subject:("x" OR "y")`` group; the
groups are ANDed. Reading a filter back parses those ``from:``/``subject:`` operators, so
rules created here round-trip; a foreign free-form query (no such operators) is preserved
for display only.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from rwz_importer.api.capabilities import ProviderCapabilities
from rwz_importer.api.errors import MailboxApiError
from rwz_importer.api.owa_client import ExistingRule, Folder, Message

logger = logging.getLogger("rwz_importer.api")

DEFAULT_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/"
_MAX_RETRIES = 5
_INBOX = "INBOX"
# Matches `from:` / `subject:` operators (quoted phrase or bare token) inside a Gmail query.
_FROM_SUBJECT_RE = re.compile(r'\b(from|subject):("(?:[^"\\]|\\.)*"|[^\s()]+)', re.IGNORECASE)

# Gmail semantics: unordered filters, no stop-processing, label destinations, and
# archive (remove INBOX) as a separate per-rule choice. Exposed as a constant so the
# registry / web UI can read capabilities without a token.
CAPABILITIES = ProviderCapabilities(
    ordered_rules=False,
    stop_processing=False,
    destination_kind="label",
    archive_toggle=True,
    hierarchical_folders=False,
)


class GmailApiError(MailboxApiError):
    """A Gmail REST call failed. Carries the HTTP status and response body."""


def _quote_term(term: str) -> str:
    """Quote a query term only when it has whitespace or query-significant characters.

    Bare addresses/words (``a@x.com``) stay unquoted; phrases (``Accepted:``, ``Hello world``)
    are quoted so Gmail treats them as one term.
    """
    if term and not re.search(r'[\s():"]', term):
        return term
    return '"' + term.replace('"', '\\"') + '"'


def _query_group(field: str, terms: list[str]) -> str:
    """Build a ``field:(a OR b)`` group (or bare ``field:a`` for a single term)."""
    parts = [f"{field}:{_quote_term(t.strip())}" for t in terms if t and t.strip()]
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


def _build_query(sender_terms: list[str], subject_terms: list[str]) -> str:
    """Compose a Gmail search query: from-group AND subject-group (each OR-joined)."""
    groups = [g for g in (_query_group("from", sender_terms), _query_group("subject", subject_terms)) if g]
    return " ".join(groups)


def _parse_query(query: str | None) -> tuple[list[str], list[str]]:
    """Extract ``from:``/``subject:`` terms from a query — round-trips our own output."""
    froms: list[str] = []
    subjects: list[str] = []
    for field, raw in _FROM_SUBJECT_RE.findall(query or ""):
        term = raw[1:-1].replace('\\"', '"') if raw.startswith('"') and raw.endswith('"') else raw
        (froms if field.lower() == "from" else subjects).append(term)
    return froms, subjects


def _label_name_from_id(label_id: str | None, names: dict[str, str]) -> str | None:
    if not label_id:
        return None
    return names.get(label_id, label_id)


@dataclass
class _GmailFilter:
    """Light view of a Gmail filter for parsing into an ExistingRule."""

    id: str
    from_terms: list[str] = field(default_factory=list)
    subject_terms: list[str] = field(default_factory=list)
    raw_query: str | None = None
    add_label_ids: list[str] = field(default_factory=list)
    remove_label_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_api(cls, d: dict) -> "_GmailFilter":
        criteria = d.get("criteria") or {}
        action = d.get("action") or {}
        from_terms: list[str] = []
        subject_terms: list[str] = []
        # Structured fields hold one plain phrase each (filters made in the Gmail UI).
        if (criteria.get("from") or "").strip():
            from_terms.append(criteria["from"].strip())
        if (criteria.get("subject") or "").strip():
            subject_terms.append(criteria["subject"].strip())
        # The query field (what we write) carries from:/subject: operators — parse them back.
        q_from, q_subject = _parse_query(criteria.get("query"))
        from_terms += q_from
        subject_terms += q_subject
        # A free-form query with no from:/subject: operators is kept only for display.
        raw_query = criteria.get("query") if (criteria.get("query") and not q_from and not q_subject) else None
        return cls(
            id=d.get("id", ""),
            from_terms=from_terms,
            subject_terms=subject_terms,
            raw_query=raw_query,
            add_label_ids=list(action.get("addLabelIds") or []),
            remove_label_ids=list(action.get("removeLabelIds") or []),
        )


class GmailClient:
    """Minimal Gmail client for labels (folders), filters (rules) and messages."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        sleep=time.sleep,
    ):
        if not token:
            raise ValueError("A Google OAuth access token is required.")
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        # Cache of user label name -> id (and id -> name), filled on first listing.
        self._label_names: dict[str, str] = {}

    def __enter__(self) -> "GmailClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # --------------------------------------------------------- Capabilities --
    def capabilities(self) -> ProviderCapabilities:
        """Gmail: unordered filters, no stop, label destinations, archive is separate."""
        return CAPABILITIES

    # ----------------------------------------------------------------- HTTP --
    def _request(self, method: str, url: str, *, json: dict | None = None) -> httpx.Response:
        """Issue one request, retrying on 429/503 with exponential backoff."""
        attempt = 0
        while True:
            response = self._client.request(method, url, json=json)
            if response.status_code in (429, 503) and attempt < _MAX_RETRIES:
                wait = _retry_after_seconds(response, default=2 ** attempt)
                logger.warning("%s from %s %s; retrying in %.1fs", response.status_code, method, url, wait)
                self._sleep(wait)
                attempt += 1
                continue
            logger.info("%s %s -> %s", method, url, response.status_code)
            if response.status_code >= 400:
                body = _safe_body(response)
                logger.error("%s %s failed (%s): %s", method, url, response.status_code, body)
                raise GmailApiError(
                    f"{method} {url} failed with HTTP {response.status_code}",
                    status=response.status_code,
                    body=body,
                )
            return response

    def _get_paged(self, url: str, *, key: str, limit: int | None = None) -> list[dict]:
        """GET a collection, following ``nextPageToken`` until exhausted or ``limit``."""
        items: list[dict] = []
        page_token: str | None = None
        while True:
            sep = "&" if "?" in url else "?"
            paged = url if page_token is None else f"{url}{sep}pageToken={page_token}"
            payload = self._request("GET", paged).json()
            items.extend(payload.get(key, []) or [])
            page_token = payload.get("nextPageToken")
            if not page_token or (limit is not None and len(items) >= limit):
                break
        return items[:limit] if limit is not None else items

    # -------------------------------------------------------------- Folders --
    def _refresh_label_names(self) -> list[dict]:
        payload = self._request("GET", "labels").json()
        labels = payload.get("labels", []) or []
        self._label_names = {lb.get("id", ""): lb.get("name", "") for lb in labels}
        return labels

    def _user_labels(self) -> list[Folder]:
        """User-created labels as folders (system labels like INBOX are not destinations)."""
        labels = self._refresh_label_names()
        return [
            Folder(id=lb.get("id", ""), display_name=lb.get("name", ""))
            for lb in labels
            if lb.get("type") == "user" and lb.get("id") and lb.get("name")
        ]

    def list_inbox_folders(self) -> list[Folder]:
        # Gmail labels are flat; list_all_folders returns the full set. Returning an
        # empty list here avoids double-listing in the service's _collect_folders.
        return []

    def list_all_folders(self) -> list[Folder]:
        return self._user_labels()

    def list_child_folders(self, parent_id: str) -> list[Folder]:
        # Labels have no real hierarchy (the "/" is cosmetic), so there are no children.
        return []

    def create_folder(self, display_name: str) -> Folder:
        response = self._request("POST", "labels", json={"name": display_name})
        data = response.json()
        if data.get("id"):
            self._label_names[data["id"]] = data.get("name", display_name)
        return Folder(id=data.get("id", ""), display_name=data.get("name", display_name))

    # ---------------------------------------------------------------- Rules --
    def list_rules(self) -> list[ExistingRule]:
        if not self._label_names:
            self._refresh_label_names()
        filters = self._get_paged("settings/filters", key="filter")
        return [self._existing_rule(_GmailFilter.from_api(f)) for f in filters]

    def _existing_rule(self, f: _GmailFilter) -> ExistingRule:
        # First add-label that is a real (user) label is the destination.
        dest_id = next((lid for lid in f.add_label_ids if lid != _INBOX), None)
        subjects = list(f.subject_terms)
        if not f.from_terms and not subjects and f.raw_query:
            subjects = [f.raw_query]  # surface a foreign free-form query for display
        return ExistingRule(
            id=f.id,
            display_name=_synthesize_name(f.from_terms, subjects, self._label_names.get(dest_id or "")),
            sequence=None,  # Gmail filters are unordered
            is_enabled=True,  # Gmail filters are always active
            from_addresses=list(f.from_terms),
            subject_contains=subjects,
            sender_contains=[],  # Gmail can't distinguish exact vs contains; all in from_addresses
            move_to_folder_id=dest_id,
            stop_processing=False,
            archive=_INBOX in f.remove_label_ids,
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
        """Create a Gmail filter. ``sequence``/``stop_processing`` are ignored (no
        ordering on Gmail); ``display_name`` has no Gmail field and is not stored."""
        sender_terms = [*(from_addresses or []), *(sender_contains or [])]
        # Gmail's structured criteria.from/subject are plain phrase fields with no boolean
        # support, so multi-value conditions must go through criteria.query (proper search
        # syntax: from:/subject: operators, OR, quoted phrases). We use query uniformly.
        query = _build_query(sender_terms, list(subject_contains or []))
        criteria: dict = {"query": query} if query else {}
        if not criteria:
            raise ValueError("A Gmail filter needs at least one sender or subject condition.")

        add_labels = [folder_id] if folder_id else []
        remove_labels = [_INBOX] if archive else []
        if not add_labels and not remove_labels:
            raise ValueError("A Gmail filter needs a destination label or archive action.")
        action = {"addLabelIds": add_labels, "removeLabelIds": remove_labels}

        response = self._request(
            "POST", "settings/filters", json={"criteria": criteria, "action": action}
        )
        created = _GmailFilter.from_api(response.json())
        if not self._label_names:
            self._refresh_label_names()
        return self._existing_rule(created)

    def delete_rule(self, rule_id: str) -> None:
        """Delete a Gmail filter by id (expects HTTP 204)."""
        if not rule_id:
            raise ValueError("A filter id is required to delete a Gmail filter.")
        self._request("DELETE", f"settings/filters/{rule_id}")

    # ------------------------------------------------------------- Messages --
    def list_messages(self, *, folder: str = "Inbox", top: int = 200) -> list[Message]:
        """Read recent message metadata (sender/subject/date — never bodies).

        Gmail returns only ids from a search, so each id is fetched with
        ``format=metadata`` for just the From/Subject/Date headers. ``folder`` is a
        Gmail search scope: ``Inbox`` -> ``in:inbox``; any other name -> ``label:"name"``.
        """
        top = max(1, top)
        query = "in:inbox" if folder.strip().lower() in ("inbox", "") else f'label:"{folder}"'
        listed = self._get_paged(
            f"messages?q={quote(query, safe='')}&maxResults=100",
            key="messages",
            limit=top,
        )
        messages: list[Message] = []
        for stub in listed[:top]:
            mid = stub.get("id")
            if not mid:
                continue
            url = (
                f"messages/{mid}?format=metadata"
                "&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date"
            )
            messages.append(_message_from_api(self._request("GET", url).json()))
        return messages

    def move_message(self, message_id: str, destination_folder_id: str) -> str:
        """Move a message: apply the destination label and remove it from the Inbox.

        Gmail ``modify`` keeps the same message id (unlike OWA's move), so the
        original id is returned.
        """
        if not message_id:
            raise ValueError("A message id is required to move a message.")
        if not destination_folder_id:
            raise ValueError("A destination label id is required to move a message.")
        response = self._request(
            "POST",
            f"messages/{message_id}/modify",
            json={"addLabelIds": [destination_folder_id], "removeLabelIds": [_INBOX]},
        )
        return (response.json() or {}).get("id", message_id)


def _synthesize_name(from_terms: list[str], subject_terms: list[str], dest_name: str | None) -> str:
    """Gmail filters have no name; build a readable one from the criteria + label."""
    if from_terms:
        cond = "From " + ", ".join(from_terms[:2]) + ("…" if len(from_terms) > 2 else "")
    elif subject_terms:
        cond = "Subject " + ", ".join(subject_terms[:2]) + ("…" if len(subject_terms) > 2 else "")
    else:
        cond = "Filter"
    return f"{cond} → {dest_name}" if dest_name else cond


def _message_from_api(d: dict) -> Message:
    headers = {
        (h.get("name") or "").lower(): h.get("value", "")
        for h in ((d.get("payload") or {}).get("headers") or [])
    }
    name, address = _parse_from(headers.get("from", ""))
    return Message(
        id=d.get("id", ""),
        subject=(headers.get("subject") or "").strip(),
        sender_address=address,
        sender_name=name,
        received=headers.get("date") or d.get("internalDate"),
    )


def _parse_from(value: str) -> tuple[str, str]:
    """Split a ``"Name <addr@x>"`` (or bare address) From header into (name, address)."""
    value = (value or "").strip()
    if "<" in value and ">" in value:
        name = value[: value.index("<")].strip().strip('"').strip()
        address = value[value.index("<") + 1 : value.index(">")].strip()
        return name, address.lower()
    return "", value.lower()


def _retry_after_seconds(response: httpx.Response, *, default: float) -> float:
    value = response.headers.get("Retry-After")
    if value is None:
        return float(default)
    try:
        return float(value)
    except ValueError:
        return float(default)


def _safe_body(response: httpx.Response) -> str:
    try:
        return response.text[:2000]
    except Exception:  # pragma: no cover - defensive
        return "<unreadable body>"
