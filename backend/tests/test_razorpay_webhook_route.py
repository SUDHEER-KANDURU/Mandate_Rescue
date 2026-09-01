"""Integration test for the /api/webhooks/razorpay Flask route end-to-end:
raw-body HMAC verification -> event mapping -> DB insert, using the real Flask
test client rather than calling internal functions directly."""

import hashlib
import hmac
import importlib
import json

import pytest


def _sign(raw_body_bytes, secret):
    return hmac.new(secret.encode("utf-8"), raw_body_bytes, hashlib.sha256).hexdigest()


RAZORPAY_SECRET = "a-real-test-secret-not-a-placeholder"

SAMPLE_EVENT = {
    "event": "payment.failed",
    "created_at": 1704067200,
    "payload": {
        "payment": {"entity": {"id": "pay_route_test", "amount": 250000, "notes": {}}},
        "subscription": {
            "entity": {
                "id": "sub_route_test",
                "notes": {"customer_id": "CUSTROUTE1", "merchant_category": "emi"},
            }
        },
    },
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", RAZORPAY_SECRET)
    monkeypatch.setenv("WEBHOOK_SECRET", "test-only-webhook-secret-not-for-real-use-0123456789abcdef")
    monkeypatch.setenv("MANDATE_RESCUE_API_KEY", "test-fixture-key")
    import db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "test_mandate_rescue.db"))
    db_module.init_db()
    import app as app_module
    importlib.reload(app_module)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def test_webhook_rejects_missing_signature(client):
    raw = json.dumps(SAMPLE_EVENT).encode("utf-8")
    resp = client.post("/api/webhooks/razorpay", data=raw, content_type="application/json")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_signature"


def test_webhook_rejects_wrong_signature(client):
    raw = json.dumps(SAMPLE_EVENT).encode("utf-8")
    resp = client.post(
        "/api/webhooks/razorpay", data=raw, content_type="application/json",
        headers={"X-Razorpay-Signature": "0" * 64},
    )
    assert resp.status_code == 400


def test_webhook_accepts_valid_signature_and_creates_case(client):
    raw = json.dumps(SAMPLE_EVENT).encode("utf-8")
    sig = _sign(raw, RAZORPAY_SECRET)
    resp = client.post(
        "/api/webhooks/razorpay", data=raw, content_type="application/json",
        headers={"X-Razorpay-Signature": sig},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["created"] is True
    assert body["customer_id"] == "CUSTROUTE1"

    # The webhook endpoint needs no API key (it's authenticated by the Razorpay
    # signature, not the internal mutation gate) — but the case it created must
    # show up through the normal, key-gated read path.
    resp2 = client.get("/api/cases")
    ids = [c["customer_id"] for c in resp2.get_json()]
    assert "CUSTROUTE1" in ids


def test_webhook_tampered_body_rejected(client):
    raw = json.dumps(SAMPLE_EVENT).encode("utf-8")
    sig = _sign(raw, RAZORPAY_SECRET)
    tampered = raw.replace(b"250000", b"1")
    resp = client.post(
        "/api/webhooks/razorpay", data=tampered, content_type="application/json",
        headers={"X-Razorpay-Signature": sig},
    )
    assert resp.status_code == 400


def test_webhook_acknowledges_unhandled_event_type(client):
    event = {"event": "order.paid", "payload": {}}
    raw = json.dumps(event).encode("utf-8")
    sig = _sign(raw, RAZORPAY_SECRET)
    resp = client.post(
        "/api/webhooks/razorpay", data=raw, content_type="application/json",
        headers={"X-Razorpay-Signature": sig},
    )
    assert resp.status_code == 200
    assert resp.get_json()["skipped"] is True
