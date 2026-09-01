"""Unit tests for the internal API-key gate (security.py) and its Flask wiring."""

import importlib

import pytest

import security


def test_configured_key_is_used(monkeypatch):
    monkeypatch.setenv("MANDATE_RESCUE_API_KEY", "my-configured-key")
    assert security.get_api_key() == "my-configured-key"
    assert security.is_valid_key("my-configured-key") is True
    assert security.is_valid_key("wrong-key") is False


def test_missing_key_generates_stable_process_local_key(monkeypatch):
    monkeypatch.delenv("MANDATE_RESCUE_API_KEY", raising=False)
    security._generated_key = None  # force regeneration for this test
    k1 = security.get_api_key()
    k2 = security.get_api_key()
    assert k1 == k2  # stable within the process
    assert security.is_valid_key(k1) is True
    security._generated_key = None  # don't leak state into other tests


def test_is_valid_key_rejects_non_string_or_empty(monkeypatch):
    monkeypatch.setenv("MANDATE_RESCUE_API_KEY", "my-configured-key")
    assert security.is_valid_key(None) is False
    assert security.is_valid_key("") is False
    assert security.is_valid_key(123) is False


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("MANDATE_RESCUE_API_KEY", "test-fixture-key")
    monkeypatch.setenv("WEBHOOK_SECRET", "test-only-webhook-secret-not-for-real-use-0123456789abcdef")
    import db as db_module
    # Redirect the on-disk DB path to a throwaway file so hitting real mutating
    # routes (e.g. /api/seed) through the Flask test client never touches the
    # actual project's mandate_rescue.db. app.py does `import db`, which is the
    # same cached module object, so patching it here is enough.
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "test_mandate_rescue.db"))
    db_module.init_db()
    import app as app_module
    importlib.reload(app_module)  # pick up the env var freshly
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def test_protected_endpoint_rejects_missing_key(client):
    resp = client.post("/api/seed")
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "unauthorized"


def test_protected_endpoint_rejects_wrong_key(client):
    resp = client.post("/api/seed", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401


def test_protected_endpoint_accepts_correct_key(client):
    resp = client.post("/api/seed", headers={"X-API-Key": "test-fixture-key"})
    assert resp.status_code == 200
    assert resp.get_json()["seeded"] == 180


def test_read_only_endpoint_needs_no_key(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200


def test_healthz_needs_no_key(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"
