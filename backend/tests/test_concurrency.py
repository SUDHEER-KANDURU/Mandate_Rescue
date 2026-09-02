"""Concurrency and double-recovery protection tests.

The architecture uses SQLite with busy_timeout + UNIQUE constraints as the
concurrency mechanism, appropriate for the single-writer SQLite model documented
in README. These tests verify:

1. Two simultaneous RecoveryPipeline instances on the SAME case cannot both
   process it (the second sees terminal audit status and logs webhook_duplicate).
2. A case processed by two concurrent workers ends with exactly one score event
   and one final status (no double recovery, no double escalation).
3. The webhook_events UNIQUE constraint prevents duplicate event insertion under
   concurrent attempts.

Note: true concurrent threads are used where possible, but SQLite's serialization
model means many scenarios reduce to sequential verification. The test still exercises
the in-process duplicate detection paths that protect against double processing.
"""

import random
import threading
import tempfile
import os

import pytest

import db
import agent as agent_module
import webhook_security


def _make_case(customer_id, status="new"):
    case = {
        "customer_id": customer_id,
        "amount": 1500.0,
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


# ---------------------------------------------------------------------------
# Sequential double-processing (simulates restart / run-agent-twice)
# ---------------------------------------------------------------------------

def test_sequential_double_process_is_idempotent(empty_db):
    """Processing the same case twice sequentially must produce only 1 score event
    and 1 final status, never double-recover."""
    case = _make_case("CONC_SEQ1")
    db.insert_mandate_failure(empty_db, case)
    empty_db.commit()

    policy = agent_module.PolicyParams(use_llm=False)
    rng1 = random.Random(42)
    p1 = agent_module.RecoveryPipeline(empty_db, rng1, policy)
    p1.process_case(dict(case))
    empty_db.commit()

    after_first = db.get_case(empty_db, "CONC_SEQ1")
    assert after_first["case_status"] in ("recovered", "escalated", "rejected", "invalid")

    rng2 = random.Random(99)
    p2 = agent_module.RecoveryPipeline(empty_db, rng2, policy)
    p2.process_case(dict(after_first))
    empty_db.commit()

    after_second = db.get_case(empty_db, "CONC_SEQ1")
    # Status must not change after the second attempt
    assert after_second["case_status"] == after_first["case_status"]

    audit = db.get_audit_for_case(empty_db, "CONC_SEQ1")
    score_events = [e for e in audit if e["event_type"] == "score"]
    dup_events = [e for e in audit if e["event_type"] == "webhook_duplicate"]
    assert len(score_events) == 1, f"Expected 1 score event, got {score_events}"
    assert len(dup_events) == 1, f"Expected 1 webhook_duplicate, got {dup_events}"


def test_sequential_triple_process_stays_correct(empty_db):
    """Three sequential processes: only the first should score, the rest deduplicate."""
    case = _make_case("CONC_SEQ2")
    db.insert_mandate_failure(empty_db, case)
    empty_db.commit()

    policy = agent_module.PolicyParams(use_llm=False)
    for i in range(3):
        rng = random.Random(42 + i)
        p = agent_module.RecoveryPipeline(empty_db, rng, policy)
        current = db.get_case(empty_db, "CONC_SEQ2")
        p.process_case(dict(current))
        empty_db.commit()

    audit = db.get_audit_for_case(empty_db, "CONC_SEQ2")
    score_events = [e for e in audit if e["event_type"] == "score"]
    dup_events = [e for e in audit if e["event_type"] == "webhook_duplicate"]
    assert len(score_events) == 1
    assert len(dup_events) == 2  # two extra deliveries


# ---------------------------------------------------------------------------
# Concurrent thread test
# ---------------------------------------------------------------------------

def test_concurrent_pipeline_processes_case_exactly_once(tmp_path):
    """Two threads calling process_case simultaneously on the same case (via separate
    DB connections to a shared file) must result in exactly one score event."""
    db_path = str(tmp_path / "conc_test.db")

    # Setup: create DB and insert case using a temp connection.
    import sqlite3
    saved_path = db.DB_PATH
    db.DB_PATH = db_path
    try:
        db.init_db()
        conn_setup = db.get_connection()
        case = _make_case("CONC_THREAD1")
        db.insert_mandate_failure(conn_setup, case)
        conn_setup.commit()
        conn_setup.close()

        policy = agent_module.PolicyParams(use_llm=False)
        results = []
        errors = []

        def _worker(seed):
            try:
                conn = db.get_connection()
                try:
                    rng = random.Random(seed)
                    p = agent_module.RecoveryPipeline(conn, rng, policy)
                    current = db.get_case(conn, "CONC_THREAD1")
                    p.process_case(dict(current))
                    conn.commit()
                    results.append(seed)
                finally:
                    conn.close()
            except Exception as e:
                errors.append((seed, str(e)))

        t1 = threading.Thread(target=_worker, args=(42,))
        t2 = threading.Thread(target=_worker, args=(99,))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Worker exceptions: {errors}"

        conn_verify = db.get_connection()
        try:
            audit = db.get_audit_for_case(conn_verify, "CONC_THREAD1")
        finally:
            conn_verify.close()

        score_events = [e for e in audit if e["event_type"] == "score"]
        assert len(score_events) == 1, (
            f"Expected exactly 1 score event after concurrent processing, "
            f"got {len(score_events)}: {[e['event_type'] for e in audit]}"
        )
    finally:
        db.DB_PATH = saved_path


# ---------------------------------------------------------------------------
# webhook_events UNIQUE constraint under concurrent delivery
# ---------------------------------------------------------------------------

def test_webhook_events_unique_prevents_duplicate_insertion(empty_db):
    """Inserting the same razorpay_event_id twice must be a UNIQUE violation,
    with the second insert returning False (not raising to the caller)."""
    inserted_first = db.insert_webhook_event(empty_db, "evt_uniq_test", "hash1")
    assert inserted_first is True

    inserted_second = db.insert_webhook_event(empty_db, "evt_uniq_test", "hash2")
    assert inserted_second is False

    # Only one row should exist
    rows = empty_db.execute(
        "SELECT COUNT(*) FROM webhook_events WHERE razorpay_event_id = ?",
        ("evt_uniq_test",)
    ).fetchone()[0]
    assert rows == 1


def test_concurrent_webhook_event_claim_is_safe(tmp_path):
    """Two threads claiming the same event id simultaneously via separate file-backed
    connections must result in exactly one successful insertion."""
    db_path = str(tmp_path / "conc_webhook_test.db")
    saved_path = db.DB_PATH
    db.DB_PATH = db_path
    try:
        db.init_db()

        outcomes = []
        errors = []
        lock = threading.Lock()

        def _claim(thread_id):
            try:
                conn = db.get_connection()
                try:
                    inserted = db.insert_webhook_event(conn, "evt_concurrent", "hashX")
                    conn.commit()
                    with lock:
                        outcomes.append(("inserted" if inserted else "duplicate", thread_id))
                except Exception as inner:
                    # UNIQUE constraint race: the loser gets IntegrityError
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    with lock:
                        outcomes.append(("duplicate", thread_id))
                finally:
                    conn.close()
            except Exception as e:
                errors.append((thread_id, str(e)))

        threads = [threading.Thread(target=_claim, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"
        inserts = [o for o in outcomes if o[0] == "inserted"]
        assert len(inserts) == 1, f"Expected exactly 1 insert, got: {outcomes}"
        assert len(outcomes) == 3, f"Not all threads completed: {outcomes}"
    finally:
        db.DB_PATH = saved_path
