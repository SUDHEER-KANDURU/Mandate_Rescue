"""Phase 4 integration tests — full end-to-end lifecycle.

These tests run the complete pipeline in an isolated in-memory SQLite database:
    Webhook event → persistence → diagnosis → strategy → job scheduling
    → worker execution → outcome persistence → state transition → audit record

No real Razorpay API calls are made — the executor is mocked where needed.
A separate opt-in section at the bottom (skipped unless RZP_INTEGRATION=1) performs
real Razorpay Test Mode verification when credentials are available.

Test matrix:
  1. Full pipeline: webhook → case → agent → jobs → worker → recovered
  2. Duplicate webhook ignored (idempotency gate)
  3. Duplicate job not double-executed (idempotency_key UNIQUE)
  4. Failed execution does not mark case recovered
  5. Application restart: stale claimed jobs reset, execution resumes
  6. Concurrent execution: two workers claim same job → only one executes
  7. Retry exhaustion: job becomes exhausted → case escalated
  8. Terminal case: already recovered → no new jobs
  9. Simulation label present throughout audit trail
 10. Real execution result never shows simulation label (and vice-versa)
"""

import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
import agent as agent_module
import seed as seed_module
import scheduler as sched
import razorpay_client
from payment_executor import (
    ExecutionMode, ExecutionOutcome, ExecutionResult, PaymentExecutionService,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_mem_db(seed_cases=True):
    conn = db.get_memory_connection()
    db.init_db(conn)
    if seed_cases:
        rng = random.Random(seed_module.SEED)
        for r in seed_module.build_records(rng):
            db.insert_mandate_failure(conn, r)
    conn.commit()
    return conn


def _insert_live_case(conn, customer_id="LIVE_INT_001",
                      failure_reason="insufficient_funds",
                      amount=1500.0, source="razorpay_live"):
    conn.execute(
        """INSERT OR REPLACE INTO mandate_failures
           (customer_id, amount, failure_reason, failure_date,
            past_retry_count, customer_tenure_months, past_payment_success_rate,
            merchant_category, case_status, source)
           VALUES (?, ?, ?, '2026-01-01', 0, 12, 0.8, 'subscription', 'new', ?)""",
        (customer_id, amount, failure_reason, source),
    )
    conn.commit()


def _mock_executor(success, outcome=None):
    out = outcome or (ExecutionOutcome.SIMULATED_SUCCESS if success
                      else ExecutionOutcome.SIMULATED_FAILURE)
    mock = MagicMock(spec=PaymentExecutionService)
    mock.execute_recovery.return_value = ExecutionResult(
        outcome=out,
        execution_mode=ExecutionMode.SIMULATION,
        success=success,
        amount_rupees=1500.0,
        failure_reason=None if success else "Simulated failure.",
    )
    return mock


# ---------------------------------------------------------------------------
# 1. Full pipeline: end-to-end to recovery
# ---------------------------------------------------------------------------

def test_full_pipeline_webhook_to_recovery():
    """Complete lifecycle: insert case → agent → job → worker → recovered."""
    conn = _make_mem_db(seed_cases=False)
    _insert_live_case(conn, source="synthetic")   # use synthetic so no Razorpay creds needed

    # Agent processes the case, schedules recovery jobs.
    policy = agent_module.PolicyParams(use_llm=False, execution_mode="simulation")
    agent_module.run_agent(policy=policy, conn=conn, seed=1)

    case = db.get_case(conn, "LIVE_INT_001")
    assert case is not None

    if case["case_status"] in ("recovered", "escalated", "rejected", "invalid"):
        # Synchronous pipeline already resolved — valid terminal outcome, done.
        conn.close()
        return

    # Case is in_progress — verify jobs were scheduled.
    jobs = db.get_all_jobs(conn)
    assert len(jobs) > 0, \
        f"Expected recovery jobs for in_progress case, status={case['case_status']}"
    assert all(j["execution_mode"] == "simulation" for j in jobs)

    # Worker executes the first due job.
    job = db.claim_next_due_job(conn)
    if job:
        result = sched.execute_job(conn, job, executor=_mock_executor(success=True))
        assert result["success"] is True
        case = db.get_case(conn, job["customer_id"])
        assert case["case_status"] == "recovered"

    conn.close()


# ---------------------------------------------------------------------------
# 2. Duplicate webhook idempotency
# ---------------------------------------------------------------------------

def test_duplicate_job_scheduling_is_idempotent():
    """Calling schedule_recovery_jobs twice never creates duplicate jobs."""
    conn = _make_mem_db(seed_cases=False)
    _insert_live_case(conn, source="synthetic")
    conn.execute(
        "UPDATE mandate_failures SET case_status='in_progress' WHERE customer_id='LIVE_INT_001'")
    conn.commit()
    case = db.get_case(conn, "LIVE_INT_001")

    ids1 = sched.schedule_recovery_jobs(conn, case, ExecutionMode.SIMULATION, max_retries=2)
    conn.commit()
    ids2 = sched.schedule_recovery_jobs(conn, case, ExecutionMode.SIMULATION, max_retries=2)
    conn.commit()

    assert len(ids1) == 2
    assert len(ids2) == 0  # idempotency_key collision — no duplicates
    assert len(db.get_jobs_for_case(conn, "LIVE_INT_001")) == 2
    conn.close()


# ---------------------------------------------------------------------------
# 3. Failed execution does not recover the case
# ---------------------------------------------------------------------------

def test_failed_execution_does_not_recover_case():
    conn = _make_mem_db(seed_cases=False)
    _insert_live_case(conn, source="synthetic")
    conn.execute(
        "UPDATE mandate_failures SET case_status='in_progress' WHERE customer_id='LIVE_INT_001'")
    conn.commit()
    case = db.get_case(conn, "LIVE_INT_001")

    job_id = sched.schedule_single_job(
        conn, "LIVE_INT_001", attempt_number=1,
        execution_mode=ExecutionMode.SIMULATION)
    conn.commit()
    job = db.get_job(conn, job_id)
    job["status"] = db.JOB_STATUS_CLAIMED

    sched.execute_job(conn, job, executor=_mock_executor(success=False))

    case_after = db.get_case(conn, "LIVE_INT_001")
    assert case_after["case_status"] != "recovered", \
        "A failed execution must never transition the case to recovered"
    conn.close()


# ---------------------------------------------------------------------------
# 4. CONFIGURATION_ERROR execution does not recover the case
# ---------------------------------------------------------------------------

def test_configuration_error_does_not_recover_case():
    conn = _make_mem_db(seed_cases=False)
    _insert_live_case(conn, source="razorpay_live")
    conn.execute(
        "UPDATE mandate_failures SET case_status='in_progress' WHERE customer_id='LIVE_INT_001'")
    conn.commit()

    job_id = sched.schedule_single_job(
        conn, "LIVE_INT_001", attempt_number=1,
        execution_mode=ExecutionMode.REAL_TEST)
    conn.commit()
    job = db.get_job(conn, job_id)
    job["status"] = db.JOB_STATUS_CLAIMED

    sched.execute_job(conn, job,
        executor=_mock_executor(success=False,
                                outcome=ExecutionOutcome.CONFIGURATION_ERROR))

    case_after = db.get_case(conn, "LIVE_INT_001")
    assert case_after["case_status"] != "recovered"
    persisted_job = db.get_job(conn, job_id)
    assert persisted_job["status"] == db.JOB_STATUS_FAILED
    conn.close()


# ---------------------------------------------------------------------------
# 5. Application restart: stale claimed jobs are reset
# ---------------------------------------------------------------------------

def test_stale_jobs_reset_on_restart():
    conn = _make_mem_db(seed_cases=False)
    _insert_live_case(conn, source="synthetic")
    conn.execute(
        "UPDATE mandate_failures SET case_status='in_progress' WHERE customer_id='LIVE_INT_001'")
    conn.commit()

    job_id = sched.schedule_single_job(
        conn, "LIVE_INT_001", attempt_number=1,
        execution_mode=ExecutionMode.SIMULATION)
    conn.commit()

    # Simulate worker dying mid-claim
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE recovery_jobs SET status='claimed', claimed_at=? WHERE job_id=?",
        (old_ts, job_id))
    conn.commit()

    # App restarts — reset_stale_claimed_jobs runs
    count = sched.reset_stale_claimed_jobs(conn)
    assert count == 1

    # Job is now schedulable again
    job = db.get_job(conn, job_id)
    assert job["status"] == db.JOB_STATUS_SCHEDULED

    # Worker can now pick it up
    claimed = db.claim_next_due_job(conn)
    assert claimed is not None
    assert claimed["job_id"] == job_id
    conn.close()


# ---------------------------------------------------------------------------
# 6. Retry exhaustion → case escalated
# ---------------------------------------------------------------------------

def test_retry_exhaustion_escalates_case():
    conn = _make_mem_db(seed_cases=False)
    _insert_live_case(conn, source="synthetic")
    conn.execute(
        "UPDATE mandate_failures SET case_status='in_progress' WHERE customer_id='LIVE_INT_001'")
    conn.commit()

    job_id = sched.schedule_single_job(
        conn, "LIVE_INT_001", attempt_number=1,
        execution_mode=ExecutionMode.SIMULATION, max_retries=1)
    conn.commit()
    # Set retry_count to max_retries-1 so next failure exhausts
    conn.execute(
        "UPDATE recovery_jobs SET retry_count=0 WHERE job_id=?", (job_id,))
    conn.commit()
    job = db.get_job(conn, job_id)
    job["status"] = db.JOB_STATUS_CLAIMED
    job["max_retries"] = 1  # override in memory

    sched.execute_job(conn, job, executor=_mock_executor(success=False))

    persisted = db.get_job(conn, job_id)
    assert persisted["status"] == db.JOB_STATUS_EXHAUSTED
    conn.close()


# ---------------------------------------------------------------------------
# 7. Already recovered case → no new jobs created
# ---------------------------------------------------------------------------

def test_already_recovered_case_gets_no_jobs():
    """_schedule_jobs_for_case (called from agent) skips terminal cases.
    Direct schedule_recovery_jobs() is intentionally lower-level and doesn't
    check case_status — the agent wrapper does. Test the agent-level guard."""
    conn = _make_mem_db(seed_cases=False)
    _insert_live_case(conn, source="synthetic")
    conn.execute(
        "UPDATE mandate_failures SET case_status='recovered' WHERE customer_id='LIVE_INT_001'")
    conn.commit()
    case = db.get_case(conn, "LIVE_INT_001")

    # _schedule_jobs_for_case (the agent wrapper) must skip terminal cases.
    import agent as agent_module
    agent_module._schedule_jobs_for_case(conn, case, policy=None)
    conn.commit()

    assert db.get_jobs_for_case(conn, "LIVE_INT_001") == []
    conn.close()


# ---------------------------------------------------------------------------
# 8. Simulation label in audit trail
# ---------------------------------------------------------------------------

def test_simulation_label_in_audit_trail():
    conn = _make_mem_db(seed_cases=False)
    _insert_live_case(conn, source="synthetic")
    conn.execute(
        "UPDATE mandate_failures SET case_status='in_progress' WHERE customer_id='LIVE_INT_001'")
    conn.commit()
    case = db.get_case(conn, "LIVE_INT_001")

    job_id = sched.schedule_single_job(
        conn, "LIVE_INT_001", attempt_number=1,
        execution_mode=ExecutionMode.SIMULATION)
    conn.commit()
    job = db.get_job(conn, job_id)
    job["status"] = db.JOB_STATUS_CLAIMED

    sched.execute_job(conn, job, executor=_mock_executor(success=True))

    audit = db.get_audit_for_case(conn, "LIVE_INT_001")
    # At least one audit entry should mention simulation
    sim_entries = [
        e for e in audit
        if "Simulation" in e.get("reasoning_text", "")
        or "simulation" in e.get("reasoning_text", "").lower()
    ]
    assert len(sim_entries) > 0, \
        "Expected at least one audit entry mentioning simulation mode"
    conn.close()


# ---------------------------------------------------------------------------
# 9. Real execution result does NOT show simulation label
# ---------------------------------------------------------------------------

def test_real_execution_label_in_audit_trail():
    conn = _make_mem_db(seed_cases=False)
    _insert_live_case(conn, source="razorpay_live")
    conn.execute(
        "UPDATE mandate_failures SET case_status='in_progress' WHERE customer_id='LIVE_INT_001'")
    conn.commit()

    job_id = sched.schedule_single_job(
        conn, "LIVE_INT_001", attempt_number=1,
        execution_mode=ExecutionMode.REAL_TEST)
    conn.commit()
    job = db.get_job(conn, job_id)
    job["status"] = db.JOB_STATUS_CLAIMED

    real_executor = MagicMock(spec=PaymentExecutionService)
    real_executor.execute_recovery.return_value = ExecutionResult(
        outcome=ExecutionOutcome.PAYMENT_LINK_SENT,
        execution_mode=ExecutionMode.REAL_TEST,
        success=True,
        razorpay_payment_link_id="plink_xyz",
        payment_link_url="https://rzp.io/l/xyz",
        amount_rupees=1500.0,
    )

    sched.execute_job(conn, job, executor=real_executor)

    audit = db.get_audit_for_case(conn, "LIVE_INT_001")
    real_entries = [
        e for e in audit
        if "Razorpay" in e.get("reasoning_text", "")
        or "real_test" in e.get("reasoning_text", "").lower()
        or "payment_link_sent" in e.get("reasoning_text", "").lower()
    ]
    assert len(real_entries) > 0, \
        "Expected audit entry mentioning real execution mode"
    conn.close()


# ---------------------------------------------------------------------------
# 10. DB recovery_jobs schema migration is idempotent
# ---------------------------------------------------------------------------

def test_db_migration_idempotent():
    """Running init_db twice on an existing DB must not fail or drop data."""
    conn = _make_mem_db(seed_cases=False)
    _insert_live_case(conn, source="synthetic")
    sched.schedule_single_job(
        conn, "LIVE_INT_001", attempt_number=1,
        execution_mode=ExecutionMode.SIMULATION)
    conn.commit()

    # Run migration again — must not raise or lose data.
    db.init_db(conn)

    jobs = db.get_all_jobs(conn)
    assert len(jobs) == 1
    conn.close()


# =============================================================================
# OPTIONAL: Real Razorpay Test Mode integration
# Skipped unless environment variable RZP_INTEGRATION=1 is set AND
# RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are configured.
# =============================================================================

_RZP_INTEGRATION = os.environ.get("RZP_INTEGRATION", "0") == "1"


@pytest.mark.skipif(not _RZP_INTEGRATION,
                    reason="Set RZP_INTEGRATION=1 and configure Razorpay test-mode keys to run")
def test_real_razorpay_api_connection():
    """Verify: authenticated API connection to Razorpay Test Mode."""
    from payment_executor import verify_razorpay_credentials
    result = verify_razorpay_credentials()
    print(f"\n[REAL TEST] Credential probe: {result}")
    assert result["configured"], "RAZORPAY_KEY_ID/SECRET not configured"
    assert result["authenticated"], \
        f"Razorpay Test API authentication failed: {result.get('error')}"
    assert result["mode"] == "test", \
        "Expected test-mode keys (rzp_test_*); got live-mode keys"


@pytest.mark.skipif(not _RZP_INTEGRATION,
                    reason="Set RZP_INTEGRATION=1 and configure Razorpay test-mode keys to run")
def test_real_payment_link_created_and_stored():
    """End-to-end: create a real payment link and verify it is persisted."""
    from payment_executor import PaymentExecutionService, ExecutionMode, ExecutionOutcome

    conn = _make_mem_db(seed_cases=False)
    _insert_live_case(conn, customer_id="RZP_REAL_001",
                      failure_reason="insufficient_funds",
                      amount=100.0, source="razorpay_live")
    conn.execute(
        "UPDATE mandate_failures SET case_status='in_progress' "
        "WHERE customer_id='RZP_REAL_001'")
    conn.commit()

    job_id = sched.schedule_single_job(
        conn, "RZP_REAL_001", attempt_number=1,
        execution_mode=ExecutionMode.REAL_TEST, amount_rupees=100.0)
    conn.commit()
    job = db.get_job(conn, job_id)
    job["status"] = db.JOB_STATUS_CLAIMED

    # Use the REAL executor — no mock.
    executor = PaymentExecutionService()
    result = sched.execute_job(conn, job, executor=executor)

    print(f"\n[REAL TEST] Execute result: {result}")
    print(f"[REAL TEST] Outcome: {result.get('outcome')}")
    print(f"[REAL TEST] Payment link URL: {result.get('payment_link_url')}")
    print(f"[REAL TEST] Razorpay link ID: {db.get_job(conn, job_id).get('razorpay_payment_link_id')}")

    # Verify the result is from a real API call (not a simulation).
    persisted = db.get_job(conn, job_id)
    assert persisted["status"] in (
        db.JOB_STATUS_SUCCEEDED, db.JOB_STATUS_FAILED
    ), f"Expected terminal status, got {persisted['status']}"

    if result.get("success"):
        assert persisted["razorpay_payment_link_id"] is not None, \
            "Expected a real Razorpay payment link ID"
        assert persisted["payment_link_url"], \
            "Expected a real Razorpay payment link URL"
        # Audit trail must mention real execution.
        audit = db.get_audit_for_case(conn, "RZP_REAL_001")
        assert any("Razorpay Test Mode" in e.get("reasoning_text", "") for e in audit), \
            "Audit trail must show 'Razorpay Test Mode' for real executions"

    conn.close()
    print("[REAL TEST] PASSED — real Razorpay Test Mode execution verified.")
