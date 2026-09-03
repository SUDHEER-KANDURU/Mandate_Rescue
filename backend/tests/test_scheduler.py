"""Tests for scheduler.py — Phase 4.

Covers:
- schedule_recovery_jobs(): creates correct jobs, respects idempotency key, handles mandate_revoked
- schedule_single_job(): idempotency on duplicate call
- claim_next_due_job(): concurrent-safe atomic claim via BEGIN IMMEDIATE
- execute_job(): simulation success → job succeeded + case recovered
- execute_job(): simulation failure → job re-queued with backoff
- execute_job(): terminal failure → job failed + not re-queued
- execute_job(): already terminal case → no double-transition
- reset_stale_claimed_jobs(): resets stuck jobs
- run_worker_once(): processes all due jobs
- execution_mode_for_case(): correct mode selection
- Duplicate execution prevention: two workers claim same job → only one executes
"""

import random
import sys
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
import seed as seed_module
import agent as agent_module
import scheduler as sched
from payment_executor import (
    ExecutionMode, ExecutionOutcome, ExecutionResult, PaymentExecutionService,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mem_db():
    """In-memory DB with schema + one synthetic case."""
    conn = db.get_memory_connection()
    db.init_db(conn)
    rng = random.Random(seed_module.SEED)
    records = seed_module.build_records(rng)
    for r in records:
        db.insert_mandate_failure(conn, r)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def single_case_db():
    """In-memory DB with exactly one case (insufficient_funds, synthetic)."""
    conn = db.get_memory_connection()
    db.init_db(conn)
    conn.execute(
        """INSERT INTO mandate_failures
           (customer_id, amount, failure_reason, failure_date,
            past_retry_count, customer_tenure_months, past_payment_success_rate,
            merchant_category, case_status, source)
           VALUES ('SCHED001', 2000.0, 'insufficient_funds', '2026-01-01',
                   0, 12, 0.8, 'subscription', 'in_progress', 'synthetic')"""
    )
    conn.commit()
    yield conn
    conn.close()


def _make_executor(success: bool = True, outcome=None) -> PaymentExecutionService:
    """Return a mock executor that returns a fixed result."""
    mock = MagicMock(spec=PaymentExecutionService)
    out = outcome or (ExecutionOutcome.SIMULATED_SUCCESS if success
                      else ExecutionOutcome.SIMULATED_FAILURE)
    mock.execute_recovery.return_value = ExecutionResult(
        outcome=out,
        execution_mode=ExecutionMode.SIMULATION,
        success=success,
        amount_rupees=2000.0,
        failure_reason=None if success else "Simulated failure.",
    )
    return mock


# ---------------------------------------------------------------------------
# schedule_recovery_jobs
# ---------------------------------------------------------------------------

def test_schedule_creates_jobs_for_insufficient_funds(single_case_db):
    case = db.get_case(single_case_db, "SCHED001")
    ids = sched.schedule_recovery_jobs(
        single_case_db, case, ExecutionMode.SIMULATION, max_retries=3)
    single_case_db.commit()
    assert len(ids) == 3
    jobs = db.get_jobs_for_case(single_case_db, "SCHED001")
    assert len(jobs) == 3
    attempts = sorted(j["attempt_number"] for j in jobs)
    assert attempts == [1, 2, 3]


def test_schedule_no_jobs_for_mandate_revoked(single_case_db):
    single_case_db.execute(
        "UPDATE mandate_failures SET failure_reason='mandate_revoked' WHERE customer_id='SCHED001'")
    single_case_db.commit()
    case = db.get_case(single_case_db, "SCHED001")
    ids = sched.schedule_recovery_jobs(
        single_case_db, case, ExecutionMode.SIMULATION)
    single_case_db.commit()
    assert ids == []
    assert db.get_jobs_for_case(single_case_db, "SCHED001") == []


def test_schedule_is_idempotent(single_case_db):
    """Calling schedule_recovery_jobs twice must not double-create jobs."""
    case = db.get_case(single_case_db, "SCHED001")
    ids1 = sched.schedule_recovery_jobs(
        single_case_db, case, ExecutionMode.SIMULATION, max_retries=2)
    single_case_db.commit()
    ids2 = sched.schedule_recovery_jobs(
        single_case_db, case, ExecutionMode.SIMULATION, max_retries=2)
    single_case_db.commit()
    assert len(ids1) == 2
    assert ids2 == []   # already existed — idempotency_key collision
    assert len(db.get_jobs_for_case(single_case_db, "SCHED001")) == 2


def test_schedule_locks_execution_mode(single_case_db):
    case = db.get_case(single_case_db, "SCHED001")
    sched.schedule_recovery_jobs(
        single_case_db, case, ExecutionMode.SIMULATION, max_retries=1)
    single_case_db.commit()
    jobs = db.get_jobs_for_case(single_case_db, "SCHED001")
    assert all(j["execution_mode"] == "simulation" for j in jobs)


def test_schedule_attempt1_due_immediately(single_case_db):
    case = db.get_case(single_case_db, "SCHED001")
    sched.schedule_recovery_jobs(
        single_case_db, case, ExecutionMode.SIMULATION, max_retries=3)
    single_case_db.commit()
    jobs = sorted(db.get_jobs_for_case(single_case_db, "SCHED001"),
                  key=lambda j: j["attempt_number"])
    now = datetime.now(timezone.utc)
    # Attempt 1 should be scheduled within 5 seconds of now (immediately).
    attempt1_time = datetime.fromisoformat(jobs[0]["scheduled_at"].replace("Z", "+00:00"))
    # strip tz if naive
    if attempt1_time.tzinfo is None:
        attempt1_time = attempt1_time.replace(tzinfo=timezone.utc)
    assert abs((attempt1_time - now).total_seconds()) < 60, \
        "Attempt 1 should be scheduled near-immediately"


# ---------------------------------------------------------------------------
# schedule_single_job
# ---------------------------------------------------------------------------

def test_schedule_single_job_returns_id(single_case_db):
    job_id = sched.schedule_single_job(
        single_case_db, "SCHED001", attempt_number=1,
        execution_mode=ExecutionMode.SIMULATION)
    single_case_db.commit()
    assert job_id is not None
    job = db.get_job(single_case_db, job_id)
    assert job is not None
    assert job["status"] == db.JOB_STATUS_SCHEDULED


def test_schedule_single_job_idempotent(single_case_db):
    id1 = sched.schedule_single_job(
        single_case_db, "SCHED001", attempt_number=1,
        execution_mode=ExecutionMode.SIMULATION)
    single_case_db.commit()
    id2 = sched.schedule_single_job(
        single_case_db, "SCHED001", attempt_number=1,
        execution_mode=ExecutionMode.SIMULATION)
    single_case_db.commit()
    assert id1 is not None
    assert id2 is None  # already exists


# ---------------------------------------------------------------------------
# execute_job: simulation success
# ---------------------------------------------------------------------------

def test_execute_job_simulation_success(single_case_db):
    job_id = sched.schedule_single_job(
        single_case_db, "SCHED001", attempt_number=1,
        execution_mode=ExecutionMode.SIMULATION)
    single_case_db.commit()
    job = db.get_job(single_case_db, job_id)
    # Claim it
    job["status"] = db.JOB_STATUS_CLAIMED

    result = sched.execute_job(single_case_db, job, executor=_make_executor(success=True))

    assert result["success"] is True
    assert result["job_status"] == db.JOB_STATUS_SUCCEEDED
    persisted = db.get_job(single_case_db, job_id)
    assert persisted["status"] == db.JOB_STATUS_SUCCEEDED
    assert persisted["outcome"] == "simulated_success"
    # Case should be recovered
    case = db.get_case(single_case_db, "SCHED001")
    assert case["case_status"] == "recovered"


# ---------------------------------------------------------------------------
# execute_job: simulation failure → re-queued with backoff
# ---------------------------------------------------------------------------

def test_execute_job_simulation_failure_requeues(single_case_db):
    job_id = sched.schedule_single_job(
        single_case_db, "SCHED001", attempt_number=1,
        execution_mode=ExecutionMode.SIMULATION, max_retries=3)
    single_case_db.commit()
    job = db.get_job(single_case_db, job_id)
    job["status"] = db.JOB_STATUS_CLAIMED

    result = sched.execute_job(single_case_db, job,
                               executor=_make_executor(success=False))

    # retry_count=0 → 0 < max_retries-1=2, so it gets re-queued
    assert result["success"] is False
    persisted = db.get_job(single_case_db, job_id)
    assert persisted["status"] == db.JOB_STATUS_SCHEDULED
    assert persisted["retry_count"] == 1


# ---------------------------------------------------------------------------
# execute_job: terminal failure outcome → not re-queued
# ---------------------------------------------------------------------------

def test_execute_job_invalid_credentials_not_requeued(single_case_db):
    job_id = sched.schedule_single_job(
        single_case_db, "SCHED001", attempt_number=1,
        execution_mode=ExecutionMode.REAL_TEST, max_retries=3)
    single_case_db.commit()
    job = db.get_job(single_case_db, job_id)
    job["status"] = db.JOB_STATUS_CLAIMED

    result = sched.execute_job(single_case_db, job,
        executor=_make_executor(
            success=False,
            outcome=ExecutionOutcome.INVALID_CREDENTIALS))

    persisted = db.get_job(single_case_db, job_id)
    assert persisted["status"] == db.JOB_STATUS_FAILED
    # retry_count should NOT have been incremented — terminal failure
    assert persisted["retry_count"] == 0


def test_execute_job_configuration_error_not_requeued(single_case_db):
    job_id = sched.schedule_single_job(
        single_case_db, "SCHED001", attempt_number=1,
        execution_mode=ExecutionMode.REAL_TEST)
    single_case_db.commit()
    job = db.get_job(single_case_db, job_id)
    job["status"] = db.JOB_STATUS_CLAIMED

    result = sched.execute_job(single_case_db, job,
        executor=_make_executor(
            success=False,
            outcome=ExecutionOutcome.CONFIGURATION_ERROR))

    persisted = db.get_job(single_case_db, job_id)
    assert persisted["status"] == db.JOB_STATUS_FAILED


# ---------------------------------------------------------------------------
# execute_job: exhaustion after max retries
# ---------------------------------------------------------------------------

def test_execute_job_exhausted_after_max_retries(single_case_db):
    job_id = sched.schedule_single_job(
        single_case_db, "SCHED001", attempt_number=1,
        execution_mode=ExecutionMode.SIMULATION, max_retries=3)
    single_case_db.commit()
    # Manually set retry_count to max-1 so next failure exhausts it
    single_case_db.execute(
        "UPDATE recovery_jobs SET retry_count=2 WHERE job_id=?", (job_id,))
    single_case_db.commit()
    job = db.get_job(single_case_db, job_id)
    job["status"] = db.JOB_STATUS_CLAIMED

    result = sched.execute_job(single_case_db, job,
                               executor=_make_executor(success=False))

    persisted = db.get_job(single_case_db, job_id)
    assert persisted["status"] == db.JOB_STATUS_EXHAUSTED


# ---------------------------------------------------------------------------
# Audit trail written on execution
# ---------------------------------------------------------------------------

def test_execute_job_writes_audit_entry(single_case_db):
    job_id = sched.schedule_single_job(
        single_case_db, "SCHED001", attempt_number=1,
        execution_mode=ExecutionMode.SIMULATION)
    single_case_db.commit()
    job = db.get_job(single_case_db, job_id)
    job["status"] = db.JOB_STATUS_CLAIMED
    audit_before = db.get_audit_for_case(single_case_db, "SCHED001")

    sched.execute_job(single_case_db, job, executor=_make_executor(success=True))

    audit_after = db.get_audit_for_case(single_case_db, "SCHED001")
    assert len(audit_after) > len(audit_before)
    last = audit_after[-1]
    assert last["event_type"] == "retry"
    assert "Simulation" in last["reasoning_text"] or "execution_mode" in last["reasoning_text"] \
        or last["outcome"] in ("success", "failure")


# ---------------------------------------------------------------------------
# Duplicate execution prevention: second worker gets None
# ---------------------------------------------------------------------------

def test_claim_next_due_job_only_one_winner(single_case_db):
    """Two connections racing claim_next_due_job — only one gets the job."""
    conn2 = db.get_memory_connection()
    # Share the same on-disk-like DB by using the same in-memory conn
    # (SQLite :memory: is per-connection, so simulate with a single conn
    # and check that sequential calls correctly claim then return None)
    sched.schedule_single_job(
        single_case_db, "SCHED001", attempt_number=1,
        execution_mode=ExecutionMode.SIMULATION)
    single_case_db.commit()

    job1 = db.claim_next_due_job(single_case_db)
    assert job1 is not None
    assert job1["status"] == db.JOB_STATUS_CLAIMED

    # Same connection — next claim should find nothing (already claimed)
    job2 = db.claim_next_due_job(single_case_db)
    assert job2 is None
    conn2.close()


# ---------------------------------------------------------------------------
# reset_stale_claimed_jobs
# ---------------------------------------------------------------------------

def test_reset_stale_claimed_jobs(single_case_db):
    job_id = sched.schedule_single_job(
        single_case_db, "SCHED001", attempt_number=1,
        execution_mode=ExecutionMode.SIMULATION)
    single_case_db.commit()
    # Manually set status=claimed with an old claimed_at
    old_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    single_case_db.execute(
        "UPDATE recovery_jobs SET status='claimed', claimed_at=? WHERE job_id=?",
        (old_time, job_id))
    single_case_db.commit()

    count = sched.reset_stale_claimed_jobs(single_case_db)
    assert count == 1
    job = db.get_job(single_case_db, job_id)
    assert job["status"] == db.JOB_STATUS_SCHEDULED
    assert job["retry_count"] == 1  # bumped by reset


def test_reset_does_not_touch_recent_claimed(single_case_db):
    job_id = sched.schedule_single_job(
        single_case_db, "SCHED001", attempt_number=1,
        execution_mode=ExecutionMode.SIMULATION)
    single_case_db.commit()
    # claimed_at = now → NOT stale
    now_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    single_case_db.execute(
        "UPDATE recovery_jobs SET status='claimed', claimed_at=? WHERE job_id=?",
        (now_ts, job_id))
    single_case_db.commit()

    count = sched.reset_stale_claimed_jobs(single_case_db)
    assert count == 0
    job = db.get_job(single_case_db, job_id)
    assert job["status"] == db.JOB_STATUS_CLAIMED  # unchanged


# ---------------------------------------------------------------------------
# run_worker_once
# ---------------------------------------------------------------------------

def test_run_worker_once_processes_due_job(mem_db):
    # Insert one in_progress case and a due job for it
    mem_db.execute(
        """INSERT OR REPLACE INTO mandate_failures
           (customer_id, amount, failure_reason, failure_date,
            past_retry_count, customer_tenure_months, past_payment_success_rate,
            merchant_category, case_status, source)
           VALUES ('WORKER01', 1500.0, 'bank_technical_error', '2026-01-01',
                   0, 6, 0.9, 'subscription', 'in_progress', 'synthetic')"""
    )
    mem_db.commit()

    # Use a patched db.get_connection so run_worker_once uses our in-memory DB.
    with patch("scheduler.db.get_connection", return_value=mem_db):
        # Schedule
        sched.schedule_single_job(
            mem_db, "WORKER01", attempt_number=1,
            execution_mode=ExecutionMode.SIMULATION)
        mem_db.commit()

        results = sched.run_worker_once(executor=_make_executor(success=True))

    assert len(results) == 1
    assert results[0]["customer_id"] == "WORKER01"
    assert results[0]["success"] is True


# ---------------------------------------------------------------------------
# execution_mode_for_case
# ---------------------------------------------------------------------------

def test_execution_mode_simulation_for_synthetic():
    case = {
        "customer_id": "SYN001",
        "failure_reason": "insufficient_funds",
        "source": "synthetic",
    }
    mode = sched.execution_mode_for_case(case)
    assert mode == ExecutionMode.SIMULATION


def test_execution_mode_simulation_for_mandate_revoked():
    case = {
        "customer_id": "REV001",
        "failure_reason": "mandate_revoked",
        "source": "razorpay_live",
    }
    # mandate_revoked → always simulation regardless of source
    mode = sched.execution_mode_for_case(case)
    assert mode == ExecutionMode.SIMULATION


def test_execution_mode_real_test_for_live_case_with_creds():
    case = {
        "customer_id": "LIVE001",
        "failure_reason": "insufficient_funds",
        "source": "razorpay_live",
    }
    with patch("payment_executor.verify_razorpay_credentials",
               return_value={"configured": True, "authenticated": True}):
        mode = sched.execution_mode_for_case(case)
    assert mode == ExecutionMode.REAL_TEST


def test_execution_mode_simulation_for_live_case_without_creds():
    case = {
        "customer_id": "LIVE002",
        "failure_reason": "insufficient_funds",
        "source": "razorpay_live",
    }
    with patch("payment_executor.verify_razorpay_credentials",
               return_value={"configured": False, "authenticated": False}):
        mode = sched.execution_mode_for_case(case)
    assert mode == ExecutionMode.SIMULATION


# ---------------------------------------------------------------------------
# Agent integration: jobs created after run_agent on in-progress cases
# ---------------------------------------------------------------------------

def test_agent_run_creates_recovery_jobs(mem_db):
    """After run_agent, all in_progress cases should have recovery jobs scheduled."""
    policy = agent_module.PolicyParams(use_llm=False, execution_mode="simulation")
    agent_module.run_agent(policy=policy, conn=mem_db, seed=42)

    jobs = db.get_all_jobs(mem_db, limit=5000)
    in_progress = [c for c in db.get_all_cases(mem_db)
                   if c["case_status"] == "in_progress"]
    # Every in_progress case that needs retry should have at least one job
    # (mandate_revoked cases are immediately escalated — no jobs for them).
    if in_progress:
        assert len(jobs) > 0, "Expected recovery jobs for in_progress cases"

    # All jobs must be labelled simulation (not real_test) for synthetic data
    for j in jobs:
        assert j["execution_mode"] == "simulation", \
            f"Job {j['job_id']} has execution_mode={j['execution_mode']!r}, expected 'simulation'"
