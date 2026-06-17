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
from rwz_importer.web.app import create_app  # noqa: E402
from tests.test_service import FakeMailbox  # noqa: E402


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
    assert "rwz-importer" in res.text
    assert "Getting a token" in res.text  # the instructions panel rendered


def test_config_reports_token_set_but_never_returns_token(client: TestClient) -> None:
    res = client.post("/api/config", json={"token": "secret-abc", "mailbox": "me@x.com"})
    body = res.json()
    assert body == {"token_set": True, "mailbox": "me@x.com", "base_url": body["base_url"]}
    assert "secret-abc" not in res.text  # token value never echoed

    got = client.get("/api/config").json()
    assert got["token_set"] is True and got["mailbox"] == "me@x.com"
    assert "token" not in got  # only token_set, never the value

    cleared = client.post("/api/config", json={"clear_token": True}).json()
    assert cleared["token_set"] is False


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
