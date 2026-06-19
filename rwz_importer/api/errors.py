"""Provider-neutral API error type shared by every mailbox client.

Each concrete client (OWA, Gmail, IMAP/Sieve) raises a subclass so callers can
catch :class:`MailboxApiError` once and handle "the backend rejected this call"
uniformly, while still being able to special-case a provider if needed. Lives in
its own tiny module to stay importable from every client without a cycle.
"""

from __future__ import annotations


class MailboxApiError(RuntimeError):
    """A mailbox backend call failed. Carries the HTTP/protocol status and body."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body
