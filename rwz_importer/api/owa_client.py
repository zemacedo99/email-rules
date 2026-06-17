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

logger = logging.getLogger("rwz_importer.api")

DEFAULT_BASE_URL = "https://outlook.office.com/api/v2.0/me/"
_MAX_RETRIES = 5


class OwaApiError(RuntimeError):
    """An OWA REST call failed. Carries the HTTP status and response body."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


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

    # -------------------------------------------------------------- Folders --
    def list_inbox_folders(self) -> list[Folder]:
        items = self._get_paged("MailFolders/Inbox/childfolders?$top=100")
        return [Folder(i.get("Id", ""), i.get("DisplayName", "")) for i in items]

    def list_all_folders(self) -> list[Folder]:
        items = self._get_paged("MailFolders?$top=100")
        return [Folder(i.get("Id", ""), i.get("DisplayName", "")) for i in items]

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
    ) -> ExistingRule:
        """Create an inbox message rule. Rejects ``Sequence < 1`` before any HTTP call."""
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
