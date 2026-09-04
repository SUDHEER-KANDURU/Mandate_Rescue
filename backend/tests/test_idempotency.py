"""Idempotency: duplicate Razorpay deliveries and a second process_case must not
create a second case or rescore a finished one."""

import hashlib
import hmac
import importlib
import json
import random

import pytest

import agent as agent_module
import db
import webhook_security


def _sign(raw_body_bytes, secret):
    return hmac.new(secret.encode("utf-8"), raw_body_bytes, hashlib.sha256).hexdigest()


RAZORPAY_SECRET = "a-real-test-secret-not-a-placeholder"

SAMPLE_EVENT = {
    "id": "evt_idempotency_1",
    "event": "payment.failed",
    "created_at": 1704067200,
    "payload": {
        "payment": {"entity": {"id": "pay_idem_test", "amount": 250000, "notes": {}}},
        "subscription": {
            "entity": {
                "id": "sub_idem_test",
                "notes": {"customer_id": "CUSTIDEM1", "merchant_category": "emi"},
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


def _post_sample(client):
    raw = json.dumps(SAMPLE_EVENT).encode("utf-8")
    sig = _sign(raw, RAZORPAY_SECRET)
    return client.post(
        "/api/webhooks/razorpay", data=raw, content_type="application/json",
        headers={"X-Razorpay-Signature": sig},
    )


def test_duplicate_webhook_returns_already_processed(client):
    first = _post_sample(client)
    assert first.status_code == 200
    assert first.get_json()["created"] is True
    assert first.get_json()["customer_id"] == "CUSTIDEM1"

    second = _post_sample(client)
    assert second.status_code == 200
    body = second.get_json()
    assert body["status"] == "already_processed"
    assert body["ok"] is True

    cases = client.get("/api/cases").get_json()["cases"]
    matching = [c for c in cases if c["customer_id"] == "CUSTIDEM1"]
    assert len(matching) == 1

    trail = client.get("/api/cases/CUSTIDEM1/audit").get_json()
    types = [row["event_type"] for row in trail["audit"]]
    assert "webhook_duplicate" in types


def test_process_case_second_pass_does_not_rescore(empty_db):
    """A second process_case on an already-terminal case logs webhook_duplicate
    and must not emit another score event (the Run-agent-twice failure mode)."""
    case = {
        "customer_id": "CUSTPIPE1",
        "amount": 1500.0,
        "failure_reason": "insufficient_funds",
        "failure_date": "2026-01-15",
        "past_retry_count": 0,
        "customer_tenure_months": 12,
        "past_payment_success_rate": 0.8,
        "merchant_category": "subscription",
        "case_status": "new",
        "raw_event_type": "payment.failed",
        "mandate_limit": 5000,
        "dunning_stage": 0,
        "history_success_days": "",
        "source": "synthetic",
    }
    case["webhook_signature"] = webhook_security.sign_payload(case)
    db.insert_mandate_failure(empty_db, case)
    empty_db.commit()

    pipeline = agent_module.RecoveryPipeline(empty_db, random.Random(42))
    pipeline.process_case(dict(case))
    empty_db.commit()

    after = db.get_case(empty_db, "CUSTPIPE1")
    assert after["case_status"] in ("recovered", "escalated", "rejected")

    pipeline.process_case(dict(after))
    empty_db.commit()

    types = [row["event_type"] for row in db.get_audit_for_case(empty_db, "CUSTPIPE1")]
    assert types.count("webhook_duplicate") == 1
    assert types.count("score") == 1
    assert db.get_case(empty_db, "CUSTPIPE1")["case_status"] == after["case_status"]


def test_second_run_agent_preserves_seeded_counts(fresh_db):
    """A second full agent pass must not rescore; 139/38/3 stays pinned."""
    policy = agent_module.PolicyParams(use_llm=False)
    first = agent_module.run_agent(policy=policy, conn=fresh_db)
    assert first["status_counts"].get("recovered") == 139
    assert first["status_counts"].get("escalated") == 38
    assert first["status_counts"].get("rejected") == 3

    second = agent_module.run_agent(policy=policy, conn=fresh_db)
    assert second["status_counts"].get("recovered") == 139
    assert second["status_counts"].get("escalated") == 38
    assert second["status_counts"].get("rejected") == 3
    # Every finished case should have been acknowledged as a duplicate on pass 2.
    dupes = sum(
        1 for row in db.get_all_audit(fresh_db)
        if row["event_type"] == "webhook_duplicate"
    )
    assert dupes == 180
