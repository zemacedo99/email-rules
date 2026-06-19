"""Provider registry: the one place that turns a provider id + config into a client.

The web app, CLI, and MCP server all build their :class:`MailboxProvider` through
:func:`build_provider` instead of importing a concrete client, so adding a backend
is a change in exactly one place. :func:`provider_capabilities` and
:func:`provider_info` expose a provider's static shape (terminology, whether it has
an archive toggle, what credentials it needs) *without* a token, which the UI uses
to render itself before anyone has connected.
"""

from __future__ import annotations

from typing import Any

from rwz_importer.api import gmail_client, owa_client
from rwz_importer.api.capabilities import IMAP_CAPABILITIES, ProviderCapabilities
from rwz_importer.api.provider import MailboxProvider

# Canonical id -> aliases accepted on input.
_ALIASES = {
    "outlook": {"outlook", "owa", "exchange", "office365", "o365"},
    "gmail": {"gmail", "google"},
    "imap": {"imap", "sieve", "imap/sieve", "dovecot", "fastmail"},
}

DEFAULT_PROVIDER = "outlook"


def canonical_provider(provider: str | None) -> str:
    """Normalise a provider id/alias to its canonical key (default ``outlook``)."""
    key = (provider or DEFAULT_PROVIDER).strip().lower()
    for canon, aliases in _ALIASES.items():
        if key in aliases:
            return canon
    raise ValueError(
        f"Unknown provider {provider!r}. Choose one of: {', '.join(_ALIASES)}."
    )


# Static, token-free metadata per provider — drives the UI and credential prompts.
_INFO: dict[str, dict[str, Any]] = {
    "outlook": {
        "label": "Outlook / Exchange",
        "auth": "token",  # bearer token (paste or Microsoft sign-in)
        "default_base_url": owa_client.DEFAULT_BASE_URL,
        "capabilities": owa_client.CAPABILITIES,
    },
    "gmail": {
        "label": "Gmail",
        "auth": "token",  # OAuth access token (paste or Google sign-in)
        "default_base_url": gmail_client.DEFAULT_BASE_URL,
        "capabilities": gmail_client.CAPABILITIES,
    },
    "imap": {
        "label": "IMAP (Sieve rules)",
        "auth": "imap",  # host/username/password, not a token
        "default_base_url": "",
        "capabilities": IMAP_CAPABILITIES,
    },
}


def providers() -> list[str]:
    """Canonical ids of every supported provider, in display order."""
    return list(_INFO)


def provider_capabilities(provider: str | None) -> ProviderCapabilities:
    """The static capabilities of a provider id — no token or network needed."""
    return _INFO[canonical_provider(provider)]["capabilities"]


def provider_info(provider: str | None) -> dict[str, Any]:
    """Token-free metadata (label, auth kind, default base url, capabilities dict)."""
    canon = canonical_provider(provider)
    info = _INFO[canon]
    return {
        "id": canon,
        "label": info["label"],
        "auth": info["auth"],
        "default_base_url": info["default_base_url"],
        "capabilities": info["capabilities"].to_dict(),
    }


def all_provider_info() -> list[dict[str, Any]]:
    """`provider_info` for every provider — what the web UI lists in its selector."""
    return [provider_info(p) for p in providers()]


def build_provider(provider: str | None, config: dict[str, Any]) -> MailboxProvider:
    """Construct a live provider from its id and a config dict.

    ``config`` keys are provider-specific. Token providers (Outlook, Gmail) use
    ``token`` (required) and optional ``base_url``. Raises :class:`ValueError` if a
    required credential is missing or the provider id is unknown.
    """
    canon = canonical_provider(provider)
    base_url = (config.get("base_url") or "").strip() or _INFO[canon]["default_base_url"]
    if canon == "outlook":
        token = config.get("token")
        if not token:
            raise ValueError("An Outlook bearer token is required.")
        return owa_client.OwaClient(token, base_url=base_url)
    if canon == "gmail":
        token = config.get("token")
        if not token:
            raise ValueError("A Google OAuth access token is required.")
        return gmail_client.GmailClient(token, base_url=base_url)
    if canon == "imap":
        # Lazy import: the IMAP client pulls in the optional ``sievelib`` dependency.
        from rwz_importer.api.imap_sieve_client import ImapSieveClient

        host = (config.get("host") or "").strip()
        if not host:
            raise ValueError("An IMAP host is required.")
        return ImapSieveClient(
            host,
            port=int(config.get("port") or 993),
            username=config.get("username") or "",
            password=config.get("password") or "",
            use_ssl=bool(config.get("use_ssl", True)),
            sieve_host=(config.get("sieve_host") or "").strip() or None,
            sieve_port=int(config.get("sieve_port") or 4190),
            sieve_starttls=bool(config.get("sieve_starttls", True)),
        )
    raise ValueError(f"Provider {canon!r} has no builder.")  # pragma: no cover
