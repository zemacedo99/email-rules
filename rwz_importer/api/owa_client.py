"""Thin OWA REST client for folders and inbox message rules.

Targets ``https://outlook.office.com/api/v2.0/me/`` with a Bearer token taken
from a signed-in browser session. Two things make this endpoint particular and
are handled here:

* **PascalCase** request/response bodies (``DisplayName``, ``Sequence``,
  ``FromAddresses`` -> ``[{"EmailAddress": {"Name", "Address"}}]``) — *not* the
  camelCase of Microsoft Graph. A Graph token returns 401 here and vice-versa.
* ``Sequence`` must be ``>= 1``; a 0/None sequence is rejected locally before any
  request is sent.

Pagination (``@odata.nextLink``) and HTTP 429 (honouring ``Retry-After``) are
handled transparently. Every request is logged at one line; error bodies in full.
"""

from __future__ import annotations

import logging
import time
import unicodedata
from dataclasses import dataclass, field

import httpx

from rwz_importer.api.capabilities import ProviderCapabilities
from rwz_importer.api.errors import MailboxApiError

logger = logging.getLogger("rwz_importer.api")

DEFAULT_BASE_URL = "https://outlook.office.com/api/v2.0/me/"
_MAX_RETRIES = 5

# Exchange semantics: ordered rules, stop-processing, folder destinations, and a
# move that inherently archives (so no separate archive toggle). Exposed as a
# module constant so callers (e.g. the provider registry / web UI) can read the
# capabilities without holding a token.
CAPABILITIES = ProviderCapabilities(
    ordered_rules=True,
    stop_processing=True,
    destination_kind="folder",
    archive_toggle=False,
    hierarchical_folders=True,
)


class OwaApiError(MailboxApiError):
    """An OWA REST call failed. Carries the HTTP status and response body.

    Subclasses the provider-neutral :class:`MailboxApiError` so callers may catch
    either; ``except OwaApiError`` keeps working for OWA-specific handling.
    """


@dataclass
class ExistingRule:
    """A message rule already present on the server (PascalCase fields normalised)."""

    id: str
    display_name: str
    sequence: int | None = None
    is_enabled: bool = True
    from_addresses: list[str] = field(default_factory=list)
    subject_contains: list[str] = field(default_factory=list)
    sender_contains: list[str] = field(default_factory=list)
    move_to_folder_id: str | None = None
    stop_processing: bool = True
    # On Exchange a move always removes the message from the inbox, so "archive" is
    # implied and constant; the field exists so providers like Gmail (where applying
    # a label and archiving are independent) can express it through the same model.
    archive: bool = True

    @classmethod
    def from_api(cls, d: dict) -> "ExistingRule":
        conditions = d.get("Conditions") or {}
        actions = d.get("Actions") or {}
        froms = [
            (entry.get("EmailAddress") or {}).get("Address", "")
            for entry in (conditions.get("FromAddresses") or [])
        ]
        return cls(
            id=d.get("Id", ""),
            display_name=d.get("DisplayName", ""),
            sequence=d.get("Sequence"),
            is_enabled=bool(d.get("IsEnabled", True)),
            from_addresses=[a for a in froms if a],
            subject_contains=list(conditions.get("SubjectContains") or []),
            sender_contains=list(conditions.get("SenderContains") or []),
            move_to_folder_id=actions.get("MoveToFolder"),
            stop_processing=bool(actions.get("StopProcessingRules", False)),
        )


@dataclass
class Folder:
    id: str
    display_name: str
    child_folder_count: int = 0

    @classmethod
    def from_api(cls, d: dict) -> "Folder":
        """Normalise an OWA folder object. ``ChildFolderCount`` drives recursion."""
        try:
            child_count = int(d.get("ChildFolderCount") or 0)
        except (TypeError, ValueError):
            child_count = 0
        return cls(
            id=d.get("Id", ""),
            display_name=d.get("DisplayName", ""),
            child_folder_count=child_count,
        )


@dataclass
class Message:
    """An inbox message — **metadata only**. Bodies are never requested.

    Only the fields needed to spot filing patterns (who sent it, the subject, and
    when) are selected, so reading a sample of mail stays cheap and low-exposure.
    """

    id: str
    subject: str
    sender_address: str
    sender_name: str = ""
    received: str | None = None

    @classmethod
    def from_api(cls, d: dict) -> "Message":
        envelope = (d.get("Sender") or d.get("From") or {}).get("EmailAddress") or {}
        return cls(
            id=d.get("Id", ""),
            subject=(d.get("Subject") or "").strip(),
            sender_address=(envelope.get("Address") or "").strip(),
            sender_name=envelope.get("Name", "") or "",
            received=d.get("ReceivedDateTime"),
        )


def normalize_folder_name(name: str) -> str:
    """Map Unicode punctuation Exchange dislikes to ASCII (en/em dash -> hyphen)."""
    if not name:
        return name
    out = name.replace("–", "-").replace("—", "-")
    return unicodedata.normalize("NFC", out)


class OwaClient:
    """Minimal client for OWA mail folders and inbox message rules."""

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
            raise ValueError("An OWA bearer token is required.")
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

    def __enter__(self) -> "OwaClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # --------------------------------------------------------- Capabilities --
    def capabilities(self) -> ProviderCapabilities:
        """Exchange semantics: ordered rules, stop-processing, folders, no archive toggle."""
        return CAPABILITIES

    # ----------------------------------------------------------------- HTTP --
    def _request(self, method: str, url: str, *, json: dict | None = None) -> httpx.Response:
        """Issue one request, retrying on 429 (honouring ``Retry-After``)."""
        attempt = 0
        while True:
            response = self._client.request(method, url, json=json)
            if response.status_code == 429 and attempt < _MAX_RETRIES:
                wait = _retry_after_seconds(response, default=2 ** attempt)
                logger.warning("429 from %s %s; retrying in %.1fs", method, url, wait)
                self._sleep(wait)
                attempt += 1
                continue
            logger.info("%s %s -> %s", method, url, response.status_code)
            if response.status_code >= 400:
                body = _safe_body(response)
                logger.error("%s %s failed (%s): %s", method, url, response.status_code, body)
                raise OwaApiError(
                    f"{method} {url} failed with HTTP {response.status_code}",
                    status=response.status_code,
                    body=body,
                )
            return response

    def _get_paged(self, url: str) -> list[dict]:
        """GET a collection, following ``@odata.nextLink`` until exhausted."""
        items: list[dict] = []
        next_url: str | None = url
        while next_url:
            response = self._request("GET", next_url)
            payload = response.json()
            items.extend(payload.get("value", []))
            next_url = payload.get("@odata.nextLink")
        return items

    def _get_paged_capped(self, url: str, limit: int) -> list[dict]:
        """Like :meth:`_get_paged` but stops once ``limit`` items are collected."""
        items: list[dict] = []
        next_url: str | None = url
        while next_url and len(items) < limit:
            response = self._request("GET", next_url)
            payload = response.json()
            items.extend(payload.get("value", []))
            next_url = payload.get("@odata.nextLink")
        return items[:limit]

    # -------------------------------------------------------------- Folders --
    def list_inbox_folders(self) -> list[Folder]:
        items = self._get_paged("MailFolders/Inbox/childfolders?$top=100")
        return [Folder.from_api(i) for i in items]

    def list_all_folders(self) -> list[Folder]:
        items = self._get_paged("MailFolders?$top=100")
        return [Folder.from_api(i) for i in items]

    def list_child_folders(self, parent_id: str) -> list[Folder]:
        """List the direct child folders of ``parent_id`` (one level, paginated).

        OWA returns only one level per call; :func:`rwz_importer.service._collect_folders`
        walks these to reach folders nested two or more levels deep.
        """
        if not parent_id:
            return []
        items = self._get_paged(f"MailFolders/{parent_id}/childfolders?$top=100")
        return [Folder.from_api(i) for i in items]

    def create_folder(self, display_name: str) -> Folder:
        response = self._request(
            "POST", "MailFolders/Inbox/childfolders", json={"DisplayName": display_name}
        )
        data = response.json()
        return Folder(data.get("Id", ""), data.get("DisplayName", display_name))

    # ---------------------------------------------------------------- Rules --
    def list_rules(self) -> list[ExistingRule]:
        items = self._get_paged("MailFolders/Inbox/MessageRules")
        return [ExistingRule.from_api(i) for i in items]

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
        """Create an inbox message rule. Rejects ``Sequence < 1`` before any HTTP call.

        ``archive`` is accepted for cross-provider symmetry but ignored here: an
        Exchange ``MoveToFolder`` action inherently removes the message from the inbox.
        """
        if sequence is None or sequence < 1:
            raise ValueError(
                f"Sequence must be >= 1 (Exchange requirement), got {sequence!r}."
            )

        conditions: dict = {}
        if from_addresses:
            conditions["FromAddresses"] = [
                {"EmailAddress": {"Name": addr, "Address": addr}} for addr in from_addresses
            ]
        if subject_contains:
            conditions["SubjectContains"] = list(subject_contains)
        if sender_contains:
            conditions["SenderContains"] = list(sender_contains)

        actions: dict = {"StopProcessingRules": stop_processing}
        if folder_id:
            actions["MoveToFolder"] = folder_id

        body = {
            "DisplayName": display_name,
            "Sequence": sequence,
            "IsEnabled": enabled,
            "Conditions": conditions,
            "Actions": actions,
        }
        response = self._request("POST", "MailFolders/Inbox/MessageRules", json=body)
        created = ExistingRule.from_api(response.json())
        if created.sequence is not None and created.sequence != sequence:
            logger.info(
                "Rule %r requested sequence %s, server assigned %s",
                display_name,
                sequence,
                created.sequence,
            )
        return created

    def delete_rule(self, rule_id: str) -> None:
        """Delete a message rule by id (expects HTTP 204)."""
        self._request("DELETE", f"MailFolders/Inbox/MessageRules/{rule_id}")

    # ------------------------------------------------------------- Messages --
    def list_messages(self, *, folder: str = "Inbox", top: int = 200) -> list[Message]:
        """Read recent message metadata from ``folder`` (default Inbox).

        Selects only ``Sender``/``Subject``/``ReceivedDateTime`` — message bodies are
        never fetched. ``folder`` is a well-known name (``Inbox``, ``SentItems``,
        ...) or a folder id; ``top`` caps how many messages are read.
        """
        page = min(max(top, 1), 100)
        select = "Id,Subject,Sender,ReceivedDateTime"
        url = (
            f"MailFolders/{folder}/messages"
            f"?$top={page}&$select={select}&$orderby=ReceivedDateTime%20desc"
        )
        items = self._get_paged_capped(url, top)
        return [Message.from_api(i) for i in items]

    def move_message(self, message_id: str, destination_folder_id: str) -> str:
        """Move one message to a folder; return the moved copy's new id.

        Uses ``POST messages/{id}/move`` with ``{"DestinationId": <folder id>}``.
        The move yields a fresh message in the destination, so the API returns a
        new ``Id`` (the original id no longer resolves).
        """
        if not message_id:
            raise ValueError("A message id is required to move a message.")
        if not destination_folder_id:
            raise ValueError("A destination folder id is required to move a message.")
        response = self._request(
            "POST",
            f"messages/{message_id}/move",
            json={"DestinationId": destination_folder_id},
        )
        return (response.json() or {}).get("Id", "")


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
