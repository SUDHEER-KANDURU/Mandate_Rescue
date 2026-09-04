"""Recovery job scheduler and worker — Phase 4.

DESIGN
------
This module has two responsibilities:

1. SCHEDULING  (schedule_recovery_jobs)
   Called by agent.py after the strategy decision is made for a case.
   Creates one recovery_jobs row per planned attempt with the correct
   execution_mode, scheduled_at time, and idempotency_key.  Duplicate
   calls for the same (customer_id, attempt_number) are no-ops — the
   UNIQUE idempotency_key in recovery_jobs prevents double-scheduling
   even if agent.py is called twice or the application restarts.

2. WORKER      (run_worker_once / run_worker_loop)
   Picks up due jobs, executes them via PaymentExecutionService, persists
   the result, and writes a corresponding audit_log entry.  Two workers
   racing on the same job serialize safely: claim_next_due_job uses
   BEGIN IMMEDIATE so only one worker transitions the job from
   'scheduled' → 'claimed'.

EXECUTION MODES (explicit, never implicit)
-----------------------------------------
  REAL_TEST   — calls the Razorpay Test API. Only used when
                RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are configured
                and the case source is 'razorpay_live'.
  SIMULATION  — RNG-based. Used for all synthetic cases and whenever
                real credentials are absent.

The mode is chosen in schedule_recovery_jobs() and locked into the job
row.  The worker never silently promotes a SIMULATION job to REAL_TEST
or vice-versa.

WORKER RETRY POLICY (for transient errors only)
-----------------------------------------------
  max_worker_retries  = 3   (network/5xx errors — job re-queued)
  Terminal failures (4xx, INVALID_CREDENTIALS, CONFIGURATION_ERROR)
  are never retried — they go straight to status='failed'.

APPLICATION RESTART SAFETY
---------------------------
Jobs in 'scheduled' state survive restart (they're in the DB).
Jobs stuck in 'claimed' / 'executing' (worker died mid-flight) are
reset to 'scheduled' by reset_stale_claimed_jobs() on startup so they
are not lost.  The stale window is configurable (default 15 min).

CONCURRENCY
-----------
claim_next_due_job uses BEGIN IMMEDIATE.  Two Flask workers or two
scheduler threads racing here serialize at SQLite's write lock.
Only one will see the UPDATE succeed; the other gets None back and
simply sleeps until the next tick.
"""

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import db
import agent as agent_module
import salary_window as salary_window_module
from payment_executor import (
    ExecutionMode,
    ExecutionOutcome,
    PaymentExecutionService,
    get_executor,
)

log = logging.getLogger("mandate_rescue.scheduler")

# ---------------------------------------------------------------------------
# Configuration (all overridable via environment variables)
# ---------------------------------------------------------------------------

# How long between worker poll ticks (seconds).
WORKER_POLL_INTERVAL_S = float(os.environ.get("SCHEDULER_POLL_S", "30"))

# A job stuck in 'claimed' for longer than this is considered stale and reset.
STALE_CLAIMED_WINDOW_MIN = int(os.environ.get("SCHEDULER_STALE_MIN", "15"))

# Delay between retry attempts for transient failures (seconds, per attempt).
RETRY_BACKOFF_DELAYS = [60, 300, 900]   # 1 min, 5 min, 15 min

# Non-retryable outcome codes: permanent failures — do not re-queue.
_TERMINAL_FAILURE_OUTCOMES = frozenset({
    ExecutionOutcome.INVALID_CREDENTIALS.value,
    ExecutionOutcome.CONFIGURATION_ERROR.value,
    ExecutionOutcome.INVALID_RESOURCE.value,
    ExecutionOutcome.SUBSCRIPTION_INACTIVE.value,
})

# Successful execution outcomes — mark job succeeded and case recovered.
_SUCCESS_OUTCOMES = frozenset({
    ExecutionOutcome.PAYMENT_CAPTURED.value,
    ExecutionOutcome.PAYMENT_LINK_SENT.value,
    ExecutionOutcome.SIMULATED_SUCCESS.value,
    ExecutionOutcome.SUBSCRIPTION_ACTIVE.value,
    ExecutionOutcome.PENDING_CUSTOMER_ACTION.value,
})


# ---------------------------------------------------------------------------
# Scheduling: create job rows for a planned recovery
# ---------------------------------------------------------------------------

def schedule_recovery_jobs(
    conn,
    case: dict,
    execution_mode: ExecutionMode,
    max_retries: int = None,
) -> list:
    """Create recovery_jobs rows for all planned attempts for one case.

    Called by agent.py after strategy selection.  Only creates jobs for cases
    whose strategy involves retries (insufficient_funds, bank_technical_error,
    mandate_expired, over-limit).  mandate_revoked cases get no jobs — they are
    immediately escalated with no retry.

    Returns a list of job_ids created (empty if all already existed or no retry
    applies).

    Timing:
      - Attempt 1: scheduled_at = now  (execute immediately)
      - Attempt 2: scheduled_at = now + 24h  (RBI pre-debit window)
      - Attempt 3: scheduled_at = now + 48h

    These times are stored in the job row and the scheduler worker only picks up
    jobs whose scheduled_at ≤ now, so actual execution respects the timing even
    across restarts.
    """
    if max_retries is None:
        max_retries = agent_module.MAX_RETRIES

    reason = case.get("failure_reason", "")
    source = case.get("source", "synthetic")
    customer_id = case["customer_id"]

    # mandate_revoked: no retry permitted by policy — no jobs.
    if reason == "mandate_revoked":
        return []

    # Determine the execution mode for this case.
    # Real execution only for razorpay_live cases with configured credentials.
    if execution_mode == ExecutionMode.REAL_TEST:
        from payment_executor import verify_razorpay_credentials
        creds = verify_razorpay_credentials()
        if not creds["configured"] or not creds["authenticated"]:
            log.warning(
                "Real execution requested for %s but credentials not valid (%s). "
                "Falling back to SIMULATION.",
                customer_id, creds.get("error"),
            )
            execution_mode = ExecutionMode.SIMULATION

    now = datetime.now(timezone.utc)

    # Per-attempt delay schedule (hours).
    # For insufficient_funds cases, attempt 1 is scheduled at the START of the
    # salary window so the debit is timed for when funds are most likely present.
    # Other reasons use a fixed 24-h gap between attempts (RBI pre-debit window).
    # All attempts beyond max_retries=3 use (attempt-1)*24h as a safe fallback.
    ATTEMPT_DELAYS_H = {2: 24, 3: 48}  # attempt 1 timing computed below

    # Determine attempt-1 scheduled_at using salary window for insufficient_funds
    if reason == "insufficient_funds":
        try:
            window = salary_window_module.infer_window(case)
            # window['window'] = (start_day, end_day) as day-of-month offsets from now
            # Use the start of the window (first day funds are expected) as attempt-1.
            start_day = int(window.get("window", (0, 0))[0])
            window_dt = now + timedelta(days=max(0, start_day))
            # Ensure at least 25 hours from now (RBI 24-h pre-debit notice + buffer).
            min_dt = now + timedelta(hours=25)
            attempt1_dt = max(window_dt, min_dt)
        except Exception:
            attempt1_dt = now + timedelta(hours=25)
        ATTEMPT_DELAYS_H[1] = int((attempt1_dt - now).total_seconds() / 3600)
    else:
        ATTEMPT_DELAYS_H[1] = 0  # non-insufficient_funds: execute as soon as possible

    created_ids = []
    for attempt in range(1, max_retries + 1):
        delay_h = ATTEMPT_DELAYS_H.get(attempt, (attempt - 1) * 24)
        scheduled_at = (now + timedelta(hours=delay_h)).isoformat(timespec="seconds")
        job_id = str(uuid.uuid4())

        created = db.create_recovery_job(
            conn=conn,
            job_id=job_id,
            customer_id=customer_id,
            attempt_number=attempt,
            execution_mode=execution_mode.value,
            scheduled_at=scheduled_at,
            max_retries=max_retries,
            razorpay_subscription_id=case.get("razorpay_subscription_id"),
            razorpay_payment_id=case.get("razorpay_payment_id"),
            amount_rupees=float(case.get("amount", 0) or 0),
        )
        if created:
            created_ids.append(job_id)
            log.info(
                "Scheduled recovery job %s for %s attempt=%d mode=%s at=%s",
                job_id, customer_id, attempt, execution_mode.value, scheduled_at,
            )
        else:
            log.debug(
                "Job for %s attempt=%d already exists (idempotent).",
                customer_id, attempt,
            )

    return created_ids


def schedule_single_job(
    conn,
    customer_id: str,
    attempt_number: int,
    execution_mode: ExecutionMode,
    scheduled_at: str = None,
    razorpay_subscription_id: str = None,
    razorpay_payment_id: str = None,
    amount_rupees: float = None,
    max_retries: int = 3,
) -> Optional[str]:
    """Schedule exactly one job. Returns job_id or None if already exists."""
    if scheduled_at is None:
        scheduled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    job_id = str(uuid.uuid4())
    created = db.create_recovery_job(
        conn=conn,
        job_id=job_id,
        customer_id=customer_id,
        attempt_number=attempt_number,
        execution_mode=execution_mode.value,
        scheduled_at=scheduled_at,
        max_retries=max_retries,
        razorpay_subscription_id=razorpay_subscription_id,
        razorpay_payment_id=razorpay_payment_id,
        amount_rupees=amount_rupees,
    )
    return job_id if created else None


# ---------------------------------------------------------------------------
# Worker: claim and execute one job
# ---------------------------------------------------------------------------

def execute_job(conn, job: dict, executor: PaymentExecutionService = None) -> dict:
    """Execute one claimed job and persist the result.  Returns a summary dict.

    This function:
      1. Marks the job as 'executing'.
      2. Loads the case from mandate_failures.
      3. Calls PaymentExecutionService.execute_recovery().
      4. Persists the result onto the job row.
      5. Writes an audit_log entry.
      6. If successful: transitions case_status to 'recovered'.
         If failure + retries remain: re-queues the job (increments retry_count,
         resets status to 'scheduled' with backoff delay).
         If failure + retries exhausted: transitions case_status to 'escalated'.
      7. Commits.

    Returns a dict with job_id, outcome, success, customer_id, and new_status.
    """
    if executor is None:
        executor = get_executor()

    job_id     = job["job_id"]
    cust_id    = job["customer_id"]
    attempt    = job["attempt_number"]
    mode_str   = job.get("execution_mode", "simulation")
    max_ret    = int(job.get("max_retries", 3))
    retry_cnt  = int(job.get("retry_count", 0))

    try:
        exec_mode = ExecutionMode(mode_str)
    except ValueError:
        exec_mode = ExecutionMode.SIMULATION

    # Mark as executing.
    conn.execute(
        "UPDATE recovery_jobs SET status = ? WHERE job_id = ?",
        (db.JOB_STATUS_EXECUTING, job_id),
    )
    conn.commit()

    # Load the case.
    case = db.get_case(conn, cust_id)
    if case is None:
        log.error("Job %s references unknown customer_id %s.", job_id, cust_id)
        db.update_job_result(
            conn, job_id, status=db.JOB_STATUS_FAILED,
            outcome=ExecutionOutcome.API_ERROR.value,
            failure_reason=f"Case not found: {cust_id}",
        )
        conn.commit()
        return {"job_id": job_id, "outcome": "case_not_found",
                "success": False, "customer_id": cust_id}

    # Enrich case with Razorpay IDs from the job row if available.
    if job.get("razorpay_subscription_id"):
        case["razorpay_subscription_id"] = job["razorpay_subscription_id"]
    if job.get("razorpay_payment_id"):
        case["razorpay_payment_id"] = job["razorpay_payment_id"]

    # Compute success probability for simulation mode (from score).
    import scoring as scoring_module
    score, _ = scoring_module.score_case(case)
    success_prob = agent_module._success_prob(case, score)

    log.info(
        "Executing job %s for %s attempt=%d mode=%s prob=%.2f",
        job_id, cust_id, attempt, exec_mode.value, success_prob,
    )

    # Execute.
    result = executor.execute_recovery(
        case=case,
        attempt=attempt,
        execution_mode=exec_mode,
        success_prob=success_prob,
    )

    executed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    outcome_val = result.outcome.value

    # Determine final job status.
    is_terminal_failure = outcome_val in _TERMINAL_FAILURE_OUTCOMES
    is_success          = outcome_val in _SUCCESS_OUTCOMES or result.success

    if is_success:
        job_status = db.JOB_STATUS_SUCCEEDED
    elif is_terminal_failure:
        job_status = db.JOB_STATUS_FAILED
    elif retry_cnt < max_ret - 1:
        # Transient failure: re-queue with backoff.
        job_status = db.JOB_STATUS_SCHEDULED   # reset by increment_job_retry
    else:
        job_status = db.JOB_STATUS_EXHAUSTED

    # Persist result onto job row.
    db.update_job_result(
        conn, job_id,
        status=job_status,
        outcome=outcome_val,
        executed_at=executed_at,
        failure_reason=result.failure_reason,
        razorpay_payment_id=result.razorpay_payment_id,
        razorpay_payment_link_id=result.razorpay_payment_link_id,
        payment_link_url=result.payment_link_url,
        razorpay_subscription_id=result.razorpay_subscription_id,
    )

    # Write audit_log entry.
    event_type = "retry" if exec_mode == ExecutionMode.REAL_TEST else "retry"
    audit_outcome = "success" if is_success else "failure"
    _write_job_audit(conn, case, attempt, result, audit_outcome, exec_mode)

    # Update case state machine.
    if is_success and result.outcome not in (
        ExecutionOutcome.PENDING_CUSTOMER_ACTION,
        ExecutionOutcome.SUBSCRIPTION_ACTIVE,
    ):
        _try_set_case_recovered(conn, case, attempt, result)
    elif job_status in (db.JOB_STATUS_EXHAUSTED, db.JOB_STATUS_FAILED) and \
            result.outcome != ExecutionOutcome.PAYMENT_LINK_SENT:
        _try_set_case_escalated(conn, case, attempt, result)

    # Handle transient re-queue: only increment after audit/state writes.
    if job_status == db.JOB_STATUS_SCHEDULED:
        new_retry_count = db.increment_job_retry(conn, job_id)
        delay_s = RETRY_BACKOFF_DELAYS[min(new_retry_count - 1, len(RETRY_BACKOFF_DELAYS) - 1)]
        new_scheduled = (datetime.now(timezone.utc) + timedelta(seconds=delay_s)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE recovery_jobs SET scheduled_at = ? WHERE job_id = ?",
            (new_scheduled, job_id),
        )
        log.info(
            "Job %s transient failure (attempt=%d retry=%d/%d); re-queued at %s",
            job_id, attempt, new_retry_count, max_ret, new_scheduled,
        )

    conn.commit()

    log.info(
        "Job %s complete: outcome=%s success=%s status=%s",
        job_id, outcome_val, is_success, job_status,
    )
    return {
        "job_id": job_id,
        "customer_id": cust_id,
        "attempt": attempt,
        "outcome": outcome_val,
        "success": is_success,
        "execution_mode": exec_mode.value,
        "job_status": job_status,
        "razorpay_payment_id": result.razorpay_payment_id,
        "payment_link_url": result.payment_link_url,
    }


def _write_job_audit(conn, case: dict, attempt: int, result,
                     audit_outcome: str, exec_mode: ExecutionMode) -> None:
    """Append an audit_log row for this execution result."""
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    mode_label = "Razorpay Test Mode" if exec_mode == ExecutionMode.REAL_TEST \
        else "Simulation"
    action = f"Recovery attempt {attempt} via {mode_label}"
    reasoning = result.to_audit_text()
    status_after = case.get("case_status", "in_progress")
    db.insert_audit(
        conn, case["customer_id"], ts,
        "retry" if result.execution_mode == ExecutionMode.REAL_TEST else "retry",
        action, audit_outcome, attempt, reasoning, status_after,
    )


def _try_set_case_recovered(conn, case: dict, attempt: int, result) -> None:
    """Transition case to 'recovered' if not already terminal."""
    current = case.get("case_status", "in_progress")
    if current in ("recovered", "escalated", "rejected", "invalid"):
        return
    try:
        if db.is_legal_transition(current, "recovered"):
            db.update_case(conn, case["customer_id"], case_status="recovered")
            db.record_state_transition(
                conn, case["customer_id"], current, "recovered",
                triggered_by="recovery_scheduler",
            )
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            db.insert_audit(
                conn, case["customer_id"], ts, "retry",
                f"Recovery confirmed via scheduler (attempt {attempt})",
                "success", attempt,
                f"Case recovered by scheduler. {result.to_audit_text()}",
                "recovered",
            )
            case["case_status"] = "recovered"
            log.info("Case %s transitioned to recovered by scheduler.", case["customer_id"])
    except ValueError as e:
        log.warning("Could not set %s to recovered: %s", case["customer_id"], e)


def _try_set_case_escalated(conn, case: dict, attempt: int, result) -> None:
    """Transition case to 'escalated' after all attempts exhausted."""
    current = case.get("case_status", "in_progress")
    if current in ("recovered", "escalated", "rejected", "invalid"):
        return
    target = "escalated"
    if not db.is_legal_transition(current, target):
        # Try via broken_promise if needed.
        if db.is_legal_transition(current, "broken_promise") and \
                db.is_legal_transition("broken_promise", target):
            db.update_case(conn, case["customer_id"], case_status="broken_promise")
            db.record_state_transition(
                conn, case["customer_id"], current, "broken_promise",
                triggered_by="recovery_scheduler",
            )
            case["case_status"] = "broken_promise"
            current = "broken_promise"
        else:
            log.warning(
                "Cannot escalate %s from %s — no legal path.", case["customer_id"], current
            )
            return
    try:
        db.update_case(conn, case["customer_id"], case_status="escalated")
        db.record_state_transition(
            conn, case["customer_id"], current, "escalated",
            triggered_by="recovery_scheduler",
        )
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.insert_audit(
            conn, case["customer_id"], ts, "escalate",
            f"Escalated after {attempt} failed attempts (scheduler)",
            "n/a", attempt,
            f"All {attempt} recovery attempts exhausted. {result.to_audit_text()}",
            "escalated",
        )
        case["case_status"] = "escalated"
        log.info("Case %s escalated by scheduler after attempt %d.", case["customer_id"], attempt)
    except ValueError as e:
        log.warning("Could not escalate %s: %s", case["customer_id"], e)


# ---------------------------------------------------------------------------
# Worker entry points
# ---------------------------------------------------------------------------

def run_worker_once(executor: PaymentExecutionService = None) -> list:
    """Claim and execute all currently due jobs in one pass.

    Returns a list of result dicts (one per executed job).
    Safe to call from a Flask route, a cron job, or a background thread.
    """
    conn = db.get_connection()
    results = []
    try:
        while True:
            job = db.claim_next_due_job(conn)
            if job is None:
                break
            try:
                res = execute_job(conn, job, executor=executor)
                results.append(res)
            except Exception as exc:
                log.error("Unhandled error executing job %s: %s",
                          job.get("job_id"), exc, exc_info=True)
                try:
                    db.update_job_result(
                        conn, job["job_id"],
                        status=db.JOB_STATUS_FAILED,
                        outcome=ExecutionOutcome.API_ERROR.value,
                        failure_reason=f"Unhandled worker error: {exc}",
                    )
                    conn.commit()
                except Exception:
                    pass
    finally:
        conn.close()
    return results


def reset_stale_claimed_jobs(conn=None) -> int:
    """Reset jobs stuck in 'claimed'/'executing' (worker died mid-flight).

    Called on application startup so no jobs are permanently lost.
    Returns the number of jobs reset.
    """
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=STALE_CLAIMED_WINDOW_MIN)
        ).isoformat(timespec="seconds")
        cur = conn.execute(
            """
            UPDATE recovery_jobs
            SET status = ?, retry_count = retry_count + 1
            WHERE status IN (?, ?)
              AND claimed_at < ?
            """,
            (
                db.JOB_STATUS_SCHEDULED,
                db.JOB_STATUS_CLAIMED, db.JOB_STATUS_EXECUTING,
                cutoff,
            ),
        )
        count = cur.rowcount
        if count:
            log.warning("Reset %d stale claimed/executing job(s) to scheduled.", count)
        conn.commit()
        return count
    finally:
        if own:
            conn.close()


def run_worker_loop(stop_event=None, executor: PaymentExecutionService = None) -> None:
    """Long-running worker loop for background thread / separate process use.

    Polls for due jobs every WORKER_POLL_INTERVAL_S seconds.  Stops when
    stop_event is set (threading.Event) or the process is killed.
    Not used by the Flask dev server directly — triggered via
    POST /api/scheduler/run or as a separate process.
    """
    import time
    log.info("Recovery scheduler worker started (poll_interval=%.0fs).",
             WORKER_POLL_INTERVAL_S)
    reset_stale_claimed_jobs()

    while True:
        if stop_event and stop_event.is_set():
            log.info("Scheduler worker stop event received; exiting.")
            break
        try:
            results = run_worker_once(executor=executor)
            if results:
                log.info("Worker tick: executed %d job(s).", len(results))
        except Exception as exc:
            log.error("Worker tick error: %s", exc, exc_info=True)
        time.sleep(WORKER_POLL_INTERVAL_S)


# ---------------------------------------------------------------------------
# Convenience: determine execution mode for a case
# ---------------------------------------------------------------------------

def execution_mode_for_case(case: dict) -> ExecutionMode:
    """Return the appropriate execution mode for a given case.

    Rules (in priority order):
      1. mandate_revoked  → SIMULATION always (no retry anyway).
      2. source = 'razorpay_live' AND credentials configured → REAL_TEST.
      3. Everything else → SIMULATION.

    This is the single authoritative place where mode selection happens —
    agent.py and app.py call this rather than re-implementing the logic.
    """
    if case.get("failure_reason") == "mandate_revoked":
        return ExecutionMode.SIMULATION

    if case.get("source") == "razorpay_live":
        from payment_executor import verify_razorpay_credentials
        creds = verify_razorpay_credentials()
        if creds.get("configured") and creds.get("authenticated"):
            return ExecutionMode.REAL_TEST
        log.debug(
            "Case %s is razorpay_live but credentials not valid; using SIMULATION.",
            case.get("customer_id"),
        )

    return ExecutionMode.SIMULATION
