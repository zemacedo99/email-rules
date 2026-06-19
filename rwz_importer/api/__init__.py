from rwz_importer.api.capabilities import ProviderCapabilities
from rwz_importer.api.errors import MailboxApiError
from rwz_importer.api.gmail_client import GmailApiError, GmailClient
from rwz_importer.api.owa_client import (
    ExistingRule,
    Folder,
    Message,
    OwaApiError,
    OwaClient,
    normalize_folder_name,
)
from rwz_importer.api.provider import MailboxProvider
from rwz_importer.api.registry import (
    DEFAULT_PROVIDER,
    all_provider_info,
    build_provider,
    canonical_provider,
    provider_capabilities,
    provider_info,
    providers,
)

__all__ = [
    "ExistingRule",
    "Folder",
    "Message",
    "MailboxProvider",
    "MailboxApiError",
    "OwaApiError",
    "OwaClient",
    "GmailApiError",
    "GmailClient",
    "ProviderCapabilities",
    "normalize_folder_name",
    "DEFAULT_PROVIDER",
    "all_provider_info",
    "build_provider",
    "canonical_provider",
    "provider_capabilities",
    "provider_info",
    "providers",
]
