"""The provider-agnostic mailbox interface.

Everything above the API layer (the engine, the CLI, and any future agent) talks
to a :class:`MailboxProvider` rather than to OWA directly. :class:`~rwz_importer.api.owa_client.OwaClient`
is the reference implementation; a Microsoft Graph or IMAP backend could conform
to the same surface without touching callers.

The Protocol is ``runtime_checkable`` so ``isinstance(obj, MailboxProvider)`` works
for a quick structural check (it verifies method names exist, not signatures).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from rwz_importer.api.owa_client import ExistingRule, Folder, Message


@runtime_checkable
class MailboxProvider(Protocol):
    """The set of mailbox operations the tool and any agent depend on."""

    # Folders -----------------------------------------------------------------
    def list_inbox_folders(self) -> list[Folder]: ...
    def list_all_folders(self) -> list[Folder]: ...
    def create_folder(self, display_name: str) -> Folder: ...

    # Rules -------------------------------------------------------------------
    def list_rules(self) -> list[ExistingRule]: ...
    def create_rule(
        self,
        *,
        display_name: str,
        sequence: int,
        folder_id: str | None,
        from_addresses: list[str] | None = ...,
        subject_contains: list[str] | None = ...,
        sender_contains: list[str] | None = ...,
        stop_processing: bool = ...,
        enabled: bool = ...,
    ) -> ExistingRule: ...
    def delete_rule(self, rule_id: str) -> None: ...

    # Messages ----------------------------------------------------------------
    # Reads return metadata only (never bodies); the one write moves a message.
    def list_messages(self, *, folder: str = ..., top: int = ...) -> list[Message]: ...
    def move_message(self, message_id: str, destination_folder_id: str) -> str: ...
