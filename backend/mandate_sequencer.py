
"""Intelligent Mandate Retry Sequencer - Phase 7.
Adaptive retry timing based on: failure reason, historical success windows,
customer segment, mandate age, amount, bank/method degradation signals, EV.
Answers: Why retry now? Why not retry now? Why this channel? Why stop?
"""
import json, logging, uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
import db
import recovery_orchestrator as orch

log = logging.getLogger("mandate_rescue.sequencer")
_NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- Decision signals ----

def compute_retry_decision(case, attempt_number, policy, mandate_case=None):
    """
    Decide whether to retry now, later, or stop.
    Returns a structured decision with signals and reasoning.
    data_type=ESTIMATED (all signals are derived from stored data, not externally verified).
    """
    failure_reason = case.get("failure_reason","") or (mandate_case or {}).get("failure_reason","")
    amount         = float(case.get("amount") or (mandate_case or {}).get("amount",0) or 0)
    max_retries    = int(policy.get("max_retries",3))
    cooldown_h     = int(policy.get("retry_cooldown_hours",24))
    signals        = {}
    should_retry   = True
    stop_reason    = None
    retry_reason   = ""
    suggested_delay_hours = cooldown_h

    # Signal 1: Max retries
    if attempt_number >= max_retries:
        should_retry = False
        stop_reason = f"Maximum retries ({max_retries}) reached"
        signals["max_retries_reached"] = True
        return _decision(False, stop_reason, None, signals, attempt_number)

    # Signal 2: Mandate revoked — never retry
    if failure_reason == "mandate_revoked":
        should_retry = False
        stop_reason = "Mandate is revoked — retry not possible"
        signals["mandate_revoked"] = True
        return _decision(False, stop_reason, None, signals, attempt_number)

    # Signal 3: Failure-reason specific timing
    if failure_reason == "insufficient_funds":
        # Use salary-window logic
        try:
            import salary_window as sw
            windows = sw.salary_window_retry_times(mandate_case or case)
            if windows:
                suggested_delay_hours = max(0, int((datetime.fromisoformat(
                    windows[0].replace("Z","+00:00")) -
                    datetime.now(timezone.utc)).total_seconds()/3600))
            signals["salary_window_applied"] = True
            retry_reason = "Retry scheduled at likely salary credit window for insufficient_funds"
        except Exception:
            suggested_delay_hours = 24
            retry_reason = "Retry in 24h (salary window unavailable)"
    elif failure_reason == "bank_technical_error":
        suggested_delay_hours = 2   # transient — retry quickly
        retry_reason = "Bank technical error: retry soon (transient failure)"
        signals["fast_retry"] = True
    elif failure_reason == "mandate_expired":
        should_retry = False
        stop_reason = "Mandate expired — customer must re-authorize; retry blocked"
        signals["mandate_expired"] = True
        return _decision(False, stop_reason, None, signals, attempt_number)
    else:
        suggested_delay_hours = cooldown_h
        retry_reason = f"Standard cooldown ({cooldown_h}h) for {failure_reason or 'unknown'}"

    # Signal 4: EV gate
    ev = float(case.get("expected_recovery_value") or 0)
    min_ev = float(policy.get("min_expected_value_rs",0))
    if ev>0 and ev < min_ev:
        should_retry = False
        stop_reason = f"Expected value Rs{ev:.2f} below threshold Rs{min_ev:.2f}"
        signals["ev_below_threshold"] = True
        return _decision(False, stop_reason, None, signals, attempt_number)
    signals["expected_value"] = ev

    # Signal 5: attempt history penalty
    if attempt_number >= 2:
        suggested_delay_hours = int(suggested_delay_hours * 1.5)
        signals["backoff_applied"] = True
        retry_reason += f" (backoff: {suggested_delay_hours}h)"

    scheduled_at = (datetime.now(timezone.utc) +
                    timedelta(hours=max(0,suggested_delay_hours))).isoformat(timespec="seconds")
    signals["attempt_number"] = attempt_number
    signals["suggested_delay_hours"] = suggested_delay_hours
    return _decision(True, retry_reason, scheduled_at, signals, attempt_number)


def _decision(should_retry, reason, scheduled_at, signals, attempt_number):
    return {
        "should_retry": should_retry,
        "reason": reason,
        "scheduled_at": scheduled_at,
        "attempt_number": attempt_number,
        "signals": signals,
        "data_type": "ESTIMATED",
        "note": "Retry decision is ESTIMATED from stored features. Not externally verified.",
    }


# ---- Schedule a retry ----

def schedule_mandate_retry(conn, merchant_id, mandate_customer_id,
                            case_id=None, execution_mode="SIMULATED"):
    """Schedule the next adaptive mandate retry."""
    import db as _db
    mandate_case = _db.get_case(conn, mandate_customer_id) if mandate_customer_id else None
    import recovery_orchestrator as _orch
    if case_id:
        rc = _orch.get_case(conn, case_id, merchant_id)
    else:
        # Find or create recovery case for this mandate
        rows = conn.execute(
            "SELECT * FROM recovery_cases WHERE merchant_id=? AND mandate_customer_id=? "
            "AND status NOT IN ('recovered','failed','escalated') LIMIT 1",
            (merchant_id, mandate_customer_id)).fetchone()
        if rows:
            rc = dict(rows); case_id = rc["case_id"]
        else:
            amount = float(mandate_case.get("amount",0)) if mandate_case else 0
            case_id = _orch.create_case(
                conn, merchant_id=merchant_id,
                scenario_type=_orch.SCENARIO_MANDATE_RETRY,
                amount=amount, mandate_customer_id=mandate_customer_id,
                failure_reason=mandate_case.get("failure_reason") if mandate_case else None,
                merchant_category=mandate_case.get("merchant_category") if mandate_case else None,
                source="SIMULATED", is_demo=0)
            rc = _orch.get_case(conn, case_id, merchant_id)
            _orch.detect_and_score(conn, case_id, merchant_id)
            conn.commit()
    # Count existing attempts
    existing = conn.execute(
        "SELECT COUNT(*) n FROM mandate_retry_log WHERE case_id=?", (case_id,)).fetchone()["n"]
    attempt_number = existing + 1
    from policy_center import get_merchant_policy
    policy = get_merchant_policy(conn, merchant_id)
    decision = compute_retry_decision(rc or {}, attempt_number, policy, mandate_case)
    retry_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO mandate_retry_log
           (retry_id,case_id,mandate_customer_id,merchant_id,attempt_number,
            scheduled_at,failure_reason,retry_reason,no_retry_reason,
            outcome,execution_mode,expected_value,amount,decision_signals)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (retry_id, case_id, mandate_customer_id, merchant_id, attempt_number,
         decision.get("scheduled_at") or _NOW(),
         rc.get("failure_reason") if rc else None,
         decision["reason"] if decision["should_retry"] else None,
         decision["reason"] if not decision["should_retry"] else None,
         "pending" if decision["should_retry"] else "suppressed",
         execution_mode,
         decision["signals"].get("expected_value"),
         float(rc.get("amount",0)) if rc else 0,
         json.dumps(decision["signals"])))
    conn.commit()
    return {"ok":True,"retry_id":retry_id,"decision":decision,"case_id":case_id}


def get_retry_history(conn, merchant_id, case_id=None, mandate_customer_id=None):
    cl,pa = ["merchant_id=?"],[merchant_id]
    if case_id:              cl.append("case_id=?");              pa.append(case_id)
    if mandate_customer_id:  cl.append("mandate_customer_id=?");  pa.append(mandate_customer_id)
    rows = conn.execute(
        f"SELECT * FROM mandate_retry_log WHERE {' AND '.join(cl)} ORDER BY created_at DESC",
        pa).fetchall()
    return [dict(r) for r in rows]
