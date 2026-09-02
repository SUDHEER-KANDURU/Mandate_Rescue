"""Integration tests for the /api/cases/<id>/replay endpoint.

Verifies:
- unauthorized access is rejected (401)
- non-existent case returns 404
- a new (unprocessed) case gets processed on replay
- a terminal case replay is idempotent (logged as already_terminal, no double-count)
- replay response shape is correct
- audit trail and transitions are present in replay response
"""

import importlib
import json
import random

import pytest

import db
import seed as seed_module
import agent as agent_module
import webhook_security


API_KEY = "test-replay-api-key"
WEBHOOK_SECRET = "test-only-webhook-secret-not-for-real-use-0123456789abcdef"


def _make_case(customer_id="CUSTREPLAY1", status="new"):
    case = {
        "customer_id": customer_id,
        "amount": 2000.0,
        "failure_reason": "bank_technical_error",
        "failure_date": "2026-01-15",
        "past_retry_count": 0,
        "customer_tenure_months": 12,
        "past_payment_success_rate": 0.9,
        "merchant_category": "subscription",
        "case_status": status,
        "raw_event_type": "payment.failed",
        "mandate_limit": 5000,
        "dunning_stage": 0,
        "history_success_days": "",
        "source": "synthetic",
    }
    case["webhook_signature"] = webhook_security.sign_payload(case)
    return case


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("MANDATE_RESCUE_API_KEY", API_KEY)
    monkeypatch.setenv("WEBHOOK_SECRET", WEBHOOK_SECRET)
    import db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "test_replay.db"))
    db_module.init_db()
    import app as app_module
    importlib.reload(app_module)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def _headers():
    return {"X-API-Key": API_KEY}


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------

def test_replay_rejects_missing_key(client):
    case = _make_case()
    import db as db_module
    conn = db_module.get_connection()
    db_module.insert_mandate_failure(conn, case)
    conn.commit()
    conn.close()

    resp = client.post("/api/cases/CUSTREPLAY1/replay")
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "unauthorized"


def test_replay_rejects_wrong_key(client):
    resp = client.post("/api/cases/CUSTREPLAY1/replay",
                       headers={"X-API-Key": "wrong-key"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 404 on unknown case
# ---------------------------------------------------------------------------

def test_replay_404_on_unknown_case(client):
    resp = client.post("/api/cases/DOES_NOT_EXIST/replay", headers=_headers())
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Processing a new case
# ---------------------------------------------------------------------------

def test_replay_processes_new_case(client):
    import db as db_module
    case = _make_case(customer_id="REPLAYNEW1")
    conn = db_module.get_connection()
    db_module.insert_mandate_failure(conn, case)
    conn.commit()
    conn.close()

    resp = client.post("/api/cases/REPLAYNEW1/replay", headers=_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["was_already_terminal"] is False
    # The case must now be in a terminal state
    final_status = body["case"]["case_status"]
    assert final_status in ("recovered", "escalated", "rejected", "invalid"), (
        f"Unexpected status after replay: {final_status}"
    )
    # Audit trail must be non-empty
    assert len(body["audit"]) > 0
    # Transitions must be present
    assert "transitions" in body


# ---------------------------------------------------------------------------
# Idempotency: replaying a terminal case
# ---------------------------------------------------------------------------

def test_replay_terminal_case_is_idempotent(client):
    import db as db_module
    case = _make_case(customer_id="REPLAYTERM1")
    conn = db_module.get_connection()
    db_module.insert_mandate_failure(conn, case)
    conn.commit()
    conn.close()

    # First replay — processes the case
    resp1 = client.post("/api/cases/REPLAYTERM1/replay", headers=_headers())
    assert resp1.status_code == 200
    status_after_first = resp1.get_json()["case"]["case_status"]

    # Second replay — must not change status
    resp2 = client.post("/api/cases/REPLAYTERM1/replay", headers=_headers())
    assert resp2.status_code == 200
    body2 = resp2.get_json()
    assert body2["ok"] is True
    assert body2["was_already_terminal"] is True
    assert body2["case"]["case_status"] == status_after_first


def test_replay_terminal_does_not_add_score_event(client):
    """A second replay on a terminal case must not emit another score event."""
    import db as db_module
    case = _make_case(customer_id="REPLAYSCORE1")
    conn = db_module.get_connection()
    db_module.insert_mandate_failure(conn, case)
    conn.commit()
    conn.close()

    client.post("/api/cases/REPLAYSCORE1/replay", headers=_headers())
    client.post("/api/cases/REPLAYSCORE1/replay", headers=_headers())

    conn = db_module.get_connection()
    audit = db_module.get_audit_for_case(conn, "REPLAYSCORE1")
    conn.close()
    score_events = [e for e in audit if e["event_type"] == "score"]
    assert len(score_events) == 1, (
        f"Expected 1 score event, got {len(score_events)}: {score_events}"
    )
