"""Provider capability flags shared by the API clients and the service layer.

Lives in its own module so both :mod:`rwz_importer.api.provider` and the concrete
clients (which ``provider`` imports) can use it without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderCapabilities:
    """How a backend's rule model differs from the Exchange baseline.

    The service and engine consult these instead of assuming Exchange semantics,
    so the same orchestration drives ordered providers (Exchange, Sieve) and
    unordered ones (Gmail filters) alike. Defaults describe the Exchange baseline,
    which keeps any provider that predates this object behaving exactly as before.

    * ``ordered_rules`` — rules run top-to-bottom by ``sequence`` (Exchange, Sieve).
      When ``False`` (Gmail), every matching rule applies; sequence is meaningless.
    * ``stop_processing`` — a rule can halt later rules (Exchange, Sieve ``stop;``).
      When ``False`` (Gmail), there is no shadowing between rules.
    * ``destination_kind`` — ``"folder"`` (Exchange/IMAP) or ``"label"`` (Gmail);
      drives UI terminology only.
    * ``archive_toggle`` — whether "also remove from Inbox" is a *separate* choice.
      On Exchange/IMAP a move always leaves the inbox, so it is implied (``False``);
      on Gmail applying a label and archiving are independent (``True``).
    * ``hierarchical_folders`` — true folders (Exchange/IMAP) vs. flat labels whose
      ``/`` separators are display-only (Gmail).
    """

    ordered_rules: bool = True
    stop_processing: bool = True
    destination_kind: str = "folder"
    archive_toggle: bool = False
    hierarchical_folders: bool = True

    def to_dict(self) -> dict:
        return {
            "ordered_rules": self.ordered_rules,
            "stop_processing": self.stop_processing,
            "destination_kind": self.destination_kind,
            "archive_toggle": self.archive_toggle,
            "hierarchical_folders": self.hierarchical_folders,
        }


# IMAP/Sieve shares the Exchange baseline: ordered rules, stop-processing, folder
# destinations. Defined here (not in the client) so the registry/UI can read it without
# importing the IMAP client — which pulls in the optional ``sievelib`` dependency.
IMAP_CAPABILITIES = ProviderCapabilities(
    ordered_rules=True,
    stop_processing=True,
    destination_kind="folder",
    archive_toggle=False,
    hierarchical_folders=True,
)
