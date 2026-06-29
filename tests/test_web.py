"""Web-layer tests: the Starlette JSON API drives the service over a fake mailbox.

The endpoints are thin wrappers over :mod:`rwz_importer.service` (covered in
``test_service.py``); here we check the HTTP seam — routing, request/response
shapes, the token-in-memory guard (and that the token is never echoed back), the
preview-then-confirm run flow, and the offline ``.rwz`` upload. ``starlette`` is an
optional dependency, so the whole module is skipped when it isn't installed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("starlette")
pytest.importorskip("multipart")  # python-multipart, for the upload endpoint

from starlette.testclient import TestClient  # noqa: E402

from rwz_importer.api.owa_client import ExistingRule, Folder, Message  # noqa: E402
from rwz_importer.api.provider import ProviderCapabilities  # noqa: E402
from rwz_importer.web.app import _SAMPLE_ALL, _sample_top, create_app  # noqa: E402
from tests.test_service import FakeMailbox  # noqa: E402

_GMAIL_CAPS = ProviderCapabilities(
    ordered_rules=False, stop_processing=False, destination_kind="label",
    archive_toggle=True, hierarchical_folders=False,
)


@pytest.fixture
def fake() -> FakeMailbox:
    return FakeMailbox()


@pytest.fixture
def client(fake: FakeMailbox) -> TestClient:
    """A TestClient whose every request shares one in-memory mailbox."""
    return TestClient(create_app(lambda: fake))


def test_index_is_served(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "Email Rule Manager" in res.text
    assert "Sign in with Microsoft" in res.text  # the easy sign-in path rendered


def test_config_reports_token_set_but_never_returns_token(client: TestClient) -> None:
    res = client.post("/api/config", json={"token": "secret-abc", "mailbox": "me@x.com"})
    body = res.json()
    assert body["token_set"] is True and body["mailbox"] == "me@x.com"
    assert "token" not in body  # only token_set, never the value
    assert "secret-abc" not in res.text  # token value never echoed
    # Provider-aware fields the UI relies on to render terminology.
    assert body["provider"] == "outlook"
    assert body["capabilities"]["destination_kind"] == "folder"
    assert {p["id"] for p in body["providers"]} == {"outlook", "gmail", "imap"}

    got = client.get("/api/config").json()
    assert got["token_set"] is True and got["mailbox"] == "me@x.com"
    assert "token" not in got  # only token_set, never the value

    cleared = client.post("/api/config", json={"clear_token": True}).json()
    assert cleared["token_set"] is False


def test_create_rule_threads_archive_through_web() -> None:
    """A Gmail-capability backend must receive the per-rule archive choice from POST."""
    fake = FakeMailbox(capabilities=_GMAIL_CAPS)
    web = TestClient(create_app(lambda: fake))
    res = web.post(
        "/api/rules",
        json={
            "name": "News",
            "conditions": {"fromAddresses": ["a@x.com"]},
            "move_to_folder": "Newsletters",
            "archive": False,
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["created"]["archive"] is False
    assert fake.created_rules[-1]["archive"] is False


def test_imap_config_connects_without_token_and_hides_password() -> None:
    web = TestClient(create_app(lambda: FakeMailbox()))
    body = web.post("/api/config", json={"provider": "imap"}).json()
    assert body["provider"] == "imap" and body["auth"] == "imap"
    assert body["connected"] is False  # nothing configured yet

    body = web.post(
        "/api/config",
        json={"imap": {"host": "imap.example.com", "username": "u@e.com", "password": "secret-pw"}},
    ).json()
    assert body["connected"] is True  # connected with no token at all
    assert body["token_set"] is False
    assert body["imap"]["host"] == "imap.example.com"
    assert "password" not in body["imap"] and "secret-pw" not in str(body)  # never echoed
    assert body["imap_password_set"] is True

    cleared = web.post("/api/config", json={"clear_token": True}).json()
    assert cleared["connected"] is False  # Forget disconnects the IMAP account too


def test_google_start_rejects_non_gmail_provider() -> None:
    web = TestClient(create_app(lambda: FakeMailbox()))  # default provider is outlook
    assert web.post("/api/auth/google/start", json={}).status_code == 400


def test_google_client_creds_are_never_echoed() -> None:
    web = TestClient(create_app(lambda: FakeMailbox(capabilities=_GMAIL_CAPS)), base_url="http://127.0.0.1:8765")
    web.post("/api/config", json={"provider": "gmail"})
    body = web.post(
        "/api/config", json={"google_client_id": "cid-123", "google_client_secret": "sec-xyz"}
    ).json()
    assert body["google_client_set"] is True
    assert body["google_client_id"] == "cid-123"  # the id is not secret — it pre-fills the form
    assert "google_client_secret" not in body and "sec-xyz" not in str(body)  # the secret never is


def test_google_signin_flow_sets_token_without_echoing_it(monkeypatch) -> None:
    import urllib.parse as up

    web = TestClient(create_app(lambda: FakeMailbox(capabilities=_GMAIL_CAPS)), base_url="http://127.0.0.1:8765")
    web.post("/api/config", json={"provider": "gmail"})
    assert web.post("/api/auth/google/start", json={}).status_code == 400  # needs creds first
    web.post("/api/config", json={"google_client_id": "cid", "google_client_secret": "sec"})

    start = web.post("/api/auth/google/start", json={})
    assert start.status_code == 200
    q = dict(up.parse_qsl(up.urlparse(start.json()["auth_url"]).query))
    state = q["state"]
    assert q["redirect_uri"].endswith("/api/auth/google/callback")  # decoded by parse_qsl

    monkeypatch.setattr(
        "rwz_importer.auth_google.exchange_code",
        lambda **kw: {"access_token": "AT-secret", "refresh_token": "RT", "expires_in": 3600},
    )
    cb = web.get(f"/api/auth/google/callback?code=CODE&state={state}")
    assert cb.status_code == 200 and "Signed in" in cb.text
    assert "AT-secret" not in cb.text  # token never rendered in the page

    assert web.get("/api/auth/google/status").json()["token_set"] is True
    assert web.get("/api/config").json()["token_set"] is True


def test_google_callback_escapes_error_param() -> None:
    """The callback must HTML-escape Google's error param (reflected-XSS guard)."""
    web = TestClient(create_app(lambda: FakeMailbox(capabilities=_GMAIL_CAPS)), base_url="http://127.0.0.1:8765")
    web.post("/api/config", json={"provider": "gmail"})
    res = web.get("/api/auth/google/callback?error=<script>alert(1)</script>")
    assert res.status_code == 400
    assert "<script>alert(1)</script>" not in res.text  # not reflected verbatim
    assert "&lt;script&gt;" in res.text  # escaped
    assert "Content-Security-Policy" in res.headers


def test_clear_token_cancels_pending_google_signin(monkeypatch) -> None:
    """Forgetting the token mid-sign-in must stop a late callback from re-connecting."""
    import urllib.parse as up

    web = TestClient(create_app(lambda: FakeMailbox(capabilities=_GMAIL_CAPS)), base_url="http://127.0.0.1:8765")
    web.post("/api/config", json={"provider": "gmail", "google_client_id": "cid", "google_client_secret": "sec"})
    start = web.post("/api/auth/google/start", json={})
    state = dict(up.parse_qsl(up.urlparse(start.json()["auth_url"]).query))["state"]
    web.post("/api/config", json={"clear_token": True})  # cancels the in-flight sign-in
    monkeypatch.setattr(
        "rwz_importer.auth_google.exchange_code",
        lambda **kw: {"access_token": "AT", "expires_in": 3600},
    )
    cb = web.get(f"/api/auth/google/callback?code=CODE&state={state}")
    assert cb.status_code == 400  # pending was wiped, so the state no longer matches
    assert web.get("/api/config").json()["token_set"] is False


def test_google_callback_rejects_bad_state(monkeypatch) -> None:
    web = TestClient(create_app(lambda: FakeMailbox(capabilities=_GMAIL_CAPS)), base_url="http://127.0.0.1:8765")
    web.post("/api/config", json={"provider": "gmail", "google_client_id": "cid", "google_client_secret": "sec"})
    web.post("/api/auth/google/start", json={})
    cb = web.get("/api/auth/google/callback?code=CODE&state=WRONG")
    assert cb.status_code == 400 and "did not match" in cb.text
    assert web.get("/api/config").json()["token_set"] is False


def test_config_switch_provider_clears_token_and_reports_capabilities(client: TestClient) -> None:
    client.post("/api/config", json={"token": "secret-abc"})
    switched = client.post("/api/config", json={"provider": "gmail"}).json()
    assert switched["provider"] == "gmail"
    assert switched["token_set"] is False  # a token is provider-specific; switching clears it
    assert switched["capabilities"]["destination_kind"] == "label"
    assert switched["capabilities"]["archive_toggle"] is True


def test_auth_start_returns_code_but_never_the_device_secret(client, monkeypatch) -> None:
    canned = {
        "device_code": "DEV-SECRET-123",  # must stay server-side
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://microsoft.com/devicelogin",
        "verification_uri_complete": "https://microsoft.com/devicelogin?otc=ABCD-EFGH",
        "expires_in": 900, "interval": 5,
        "_client_id": "cid", "_tenant": "common",
        "_authority": "https://login.microsoftonline.com",
    }
    monkeypatch.setattr("rwz_importer.auth.start_device_code", lambda **kw: dict(canned))
    res = client.post("/api/auth/start", json={})
    assert res.status_code == 200
    body = res.json()
    assert body["user_code"] == "ABCD-EFGH"
    assert body["verification_uri"].endswith("devicelogin")
    assert "device_code" not in body
    assert "DEV-SECRET-123" not in res.text  # the polling secret never reaches the browser


def test_auth_poll_completes_sets_token_without_echoing_it(client, monkeypatch) -> None:
    monkeypatch.setattr("rwz_importer.auth.start_device_code", lambda **kw: {
        "device_code": "DEV", "user_code": "X", "verification_uri": "u",
        "expires_in": 900, "interval": 5,
        "_client_id": "cid", "_tenant": "common", "_authority": "a",
    })
    client.post("/api/auth/start", json={})  # arms the pending device-code state

    monkeypatch.setattr("rwz_importer.auth.poll_token_once", lambda **kw: {
        "status": "complete", "access_token": "SECRET-TOKEN", "expires_in": 3600,
    })
    res = client.post("/api/auth/poll", json={})
    assert res.json() == {"status": "complete", "token_set": True}
    assert "SECRET-TOKEN" not in res.text  # the token value is never echoed

    cfg = client.get("/api/config").json()
    assert cfg["token_set"] is True and "token" not in cfg


def test_auth_poll_without_a_pending_signin_is_idle(client) -> None:
    assert client.post("/api/auth/poll", json={}).json()["status"] == "idle"


def test_live_endpoint_requires_token_with_default_factory(monkeypatch) -> None:
    monkeypatch.delenv("OWA_TOKEN", raising=False)
    default_client = TestClient(create_app())  # real OWA factory, no token set
    res = default_client.get("/api/rules")
    assert res.status_code == 400
    assert res.json()["needs_token"] is True


def test_rules_crud_roundtrip(client: TestClient, fake: FakeMailbox) -> None:
    created = client.post(
        "/api/rules",
        json={
            "name": "News",
            "conditions": {"fromAddresses": ["news@shop.com"]},
            "move_to_folder": "Newsletters",
        },
    ).json()
    assert created["created"]["name"] == "News"
    assert created["created_folder"] == "Newsletters"

    listed = client.get("/api/rules").json()
    assert [r["name"] for r in listed] == ["News"]
    rule_id = listed[0]["id"]

    updated = client.put(
        f"/api/rules/{rule_id}",
        json={"name": "News", "conditions": {"fromAddresses": ["deals@shop.com"]},
              "move_to_folder": "Newsletters"},
    ).json()
    assert updated["replaced_rule_id"] == rule_id
    assert updated["updated"]["conditions"]["fromAddresses"] == ["deals@shop.com"]

    deleted = client.delete(f"/api/rules/{updated['updated']['id']}").json()
    assert deleted["name"] == "News"
    assert client.get("/api/rules").json() == []


def test_create_rule_validation_error_is_400(client: TestClient) -> None:
    res = client.post("/api/rules", json={"name": "  "})
    assert res.status_code == 400
    assert "name" in res.json()["error"].lower()


def test_folders_endpoint(fake: FakeMailbox) -> None:
    fake._folders.append(Folder(id="f1", display_name="Archive"))
    client = TestClient(create_app(lambda: fake))
    assert client.get("/api/folders").json()[0]["display_name"] == "Archive"


def test_preview_then_execute_run(monkeypatch) -> None:
    fake = FakeMailbox(
        rules=[
            ExistingRule(
                id="r1", display_name="Shop", sequence=1,
                from_addresses=["news@shop.com"], move_to_folder_id="f1",
            )
        ],
        folders=[Folder(id="f1", display_name="Shop")],
        messages=[
            Message(id="m1", subject="Deal", sender_address="news@shop.com"),
            Message(id="m2", subject="Hi", sender_address="boss@corp.com"),
        ],
    )
    client = TestClient(create_app(lambda: fake))

    preview = client.post("/api/preview-run", json={}).json()
    assert preview["total_matches"] == 1
    assert preview["moves"][0]["message_id"] == "m1"
    assert fake.moved_messages == []  # preview wrote nothing

    out = client.post("/api/execute-run", json={"moves": preview["moves"]}).json()
    assert out["applied"] is True and out["moved_count"] == 1
    assert fake.moved_messages == [("m1", "f1")]


def test_suggest_endpoint(monkeypatch) -> None:
    fake = FakeMailbox(
        messages=[
            Message(id=f"m{i}", subject="Deal", sender_address="news@shop.com")
            for i in range(5)
        ]
    )
    client = TestClient(create_app(lambda: fake))
    out = client.post("/api/suggest", json={"min_sender": 3}).json()
    assert out["sampled_messages"] == 5
    assert any(
        s["rule"]["conditions"]["fromAddresses"] == ["news@shop.com"]
        for s in out["suggestions"]
    )


def test_sample_top_maps_all_and_nonpositive_to_a_large_cap() -> None:
    assert _sample_top("all") == _SAMPLE_ALL
    assert _sample_top("ALL") == _SAMPLE_ALL  # case-insensitive
    assert _sample_top(0) == _SAMPLE_ALL      # 0 means "everything"
    assert _sample_top(-5) == _SAMPLE_ALL
    assert _sample_top(None) == 300           # absent → default
    assert _sample_top("garbage") == 300      # unparseable → default
    assert _sample_top(50) == 50              # a real cap is kept


def test_suggest_endpoint_accepts_all(fake: FakeMailbox) -> None:
    fake._messages = [
        Message(id=f"m{i}", subject="Deal", sender_address="news@shop.com")
        for i in range(5)
    ]
    client = TestClient(create_app(lambda: fake))
    out = client.post("/api/suggest", json={"top": "all", "min_sender": 3}).json()
    assert out["sampled_messages"] == 5  # "all" drains the (small) fake inbox


def test_rwz_upload_parses_and_plans(client: TestClient, rwz_path) -> None:
    data = rwz_path.read_bytes()
    parsed = client.post(
        "/api/rwz/parse",
        files={"file": ("sample_rules.rwz", data, "application/octet-stream")},
    ).json()
    assert parsed["parsed"]["rule_count"] == 13
    assert "Email LabOps" in parsed["parsed"]["flagged_cross_mailbox"]
    assert "rules_json" in parsed

    plan = client.post("/api/rwz/plan", json={"rules_json": parsed["rules_json"]}).json()
    assert plan["confirmation_token"].startswith("v1:")
    assert "summary" in plan
