"""Provider registry tests: id normalisation, capability lookup, and construction."""

from __future__ import annotations

import pytest

from rwz_importer.api import registry
from rwz_importer.api.gmail_client import GmailClient
from rwz_importer.api.owa_client import OwaClient


def test_canonical_provider_accepts_aliases() -> None:
    assert registry.canonical_provider(None) == "outlook"
    assert registry.canonical_provider("OWA") == "outlook"
    assert registry.canonical_provider("exchange") == "outlook"
    assert registry.canonical_provider("Google") == "gmail"
    with pytest.raises(ValueError):
        registry.canonical_provider("yahoo")


def test_provider_capabilities_are_token_free() -> None:
    assert registry.provider_capabilities("gmail").destination_kind == "label"
    assert registry.provider_capabilities("gmail").archive_toggle is True
    assert registry.provider_capabilities("outlook").destination_kind == "folder"
    assert registry.provider_capabilities("outlook").archive_toggle is False


def test_all_provider_info_lists_every_provider() -> None:
    info = {p["id"]: p for p in registry.all_provider_info()}
    assert set(info) == {"outlook", "gmail", "imap"}
    assert info["gmail"]["capabilities"]["destination_kind"] == "label"
    assert info["outlook"]["default_base_url"].startswith("https://outlook.office.com")
    assert info["imap"]["auth"] == "imap"  # host/username/password, not a token


def test_canonical_provider_accepts_imap_aliases() -> None:
    assert registry.canonical_provider("imap") == "imap"
    assert registry.canonical_provider("sieve") == "imap"
    assert registry.canonical_provider("fastmail") == "imap"


def test_build_imap_requires_host() -> None:
    with pytest.raises(ValueError):
        registry.build_provider("imap", {"username": "u", "password": "p"})


def test_build_provider_constructs_each_backend() -> None:
    outlook = registry.build_provider("outlook", {"token": "t"})
    gmail = registry.build_provider("gmail", {"token": "t"})
    try:
        assert isinstance(outlook, OwaClient)
        assert isinstance(gmail, GmailClient)
    finally:
        outlook.close()
        gmail.close()


def test_build_provider_requires_token() -> None:
    with pytest.raises(ValueError):
        registry.build_provider("outlook", {})
    with pytest.raises(ValueError):
        registry.build_provider("gmail", {"token": ""})
