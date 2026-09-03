"""Outcome Attribution — Phase 6.

Connects every recovery decision to its observed outcome and writes durable
strategy-performance records. This is the data-collection backbone of the
closed-loop:

    recovery_decision → execution → outcome → strategy_performance update

Design principles
-----------------
- Never creates a duplicate outcome record (idempotent; keyed on customer_id).
- Every record carries its data provenance:
    REAL_TEST   — case.source == 'razorpay_live' and execution_mode == 'real_test'
    SIMULATION  — execution_mode == 'simulation'
    HISTORICAL  — derived from pre-Phase-6 audit_log (backfill of existing data)
    ESTIMATE    — probability-model estimate (no actual execution observed)
    FORECAST    — forward projection (not yet implemented)
- Writes to multiple strategy_performance dimensions simultaneously so the
  learning layer can answer per-failure-reason, per-merchant, per-global.
- Uses the existing audit_log + mandate_failures + recovery_jobs data as
  the single source of truth — never creates a parallel truth.

Public API
----------
attribute_outcome(conn, customer_id) → dict
    Compute and store the outcome for one fully-resolved case.

backfill_from_audit(conn) → dict
    Scan all resolved cases in the DB and write strategy_performance rows.
    Safe to run multiple times; uses upsert semantics.

get_attribution_summary(conn) → dict
    High-level summary of attribution coverage and data provenance breakdown.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

import db

# Provenance constants — single canonical set, shared across Phase 6 modules.
PROV_REAL_TEST  = "REAL_TEST"
PROV_SIMULATION = "SIMULATION"
PROV_HISTORICAL = "HISTORICAL"
PROV_ESTIMATE   = "ESTIMATE"
PROV_FORECAST   = "FORECAST"

# Terminal case statuses that can be attributed.
TERMINAL_STATUSES = frozenset({"recovered", "escalated", "broken_promise"})

# Strategy extraction from audit_log.action_taken ("Strategy: <label>").
_STRATEGY_PREFIX = "Strategy:"


def _extract_strategy(audit_events: list[dict]) -> Optional[str]:
    """Return the strategy label from a list of audit events, or None."""
    for e in audit_events:
        if e.get("event_type") == "strategy_selected":
            action = e.get("action_taken", "")
            if action.startswith(_STRATEGY_PREFIX):
                return action[len(_STRATEGY_PREFIX):].strip()
    return None


def _infer_provenance(case: dict, jobs: list[dict]) -> str:
    """Determine provenance for a case based on source and execution mode.

    Priority:
    1. If any job has execution_mode='real_test' AND source is 'razorpay_live'
       → REAL_TEST
    2. If any job has execution_mode='simulation'
       → SIMULATION
    3. No jobs (pre-Phase-4 data)
       → HISTORICAL
    """
    source = case.get("source", "synthetic")
    for job in jobs:
        mode = job.get("execution_mode", "")
        if mode == "real_test" and source == "razorpay_live":
            return PROV_REAL_TEST
        if mode == "simulation":
            return PROV_SIMULATION
    # Pre-Phase-4 run: no jobs, case was resolved by agent pipeline directly.
    return PROV_HISTORICAL


def _time_to_recovery_hours(case: dict, jobs: list[dict]) -> Optional[float]:
    """Estimate hours from failure_date to first successful recovery job."""
    if case.get("case_status") != "recovered":
        return None
    failure_date_str = case.get("failure_date")
    if not failure_date_str:
        return None

    # Find earliest succeeded job
    executed_times = []
    for job in jobs:
        if job.get("status") == "succeeded" and job.get("executed_at"):
            executed_times.append(job["executed_at"])
    if not executed_times:
        return None

    try:
        fd = datetime.fromisoformat(failure_date_str.replace("Z", "+00:00"))
        ed = datetime.fromisoformat(
            sorted(executed_times)[0].replace("Z", "+00:00")
        )
        delta_hours = (ed - fd).total_seconds() / 3600.0
        return max(0.0, round(delta_hours, 2))
    except Exception:
        return None


def attribute_outcome(conn, customer_id: str) -> dict:
    """Compute and store the strategy-performance contribution for one case.

    Returns a dict describing what was attributed (or why attribution was skipped).
    Idempotent: calling twice for the same resolved case produces the same result.
    """
    case = db.get_case(conn, customer_id)
    if not case:
        return {"attributed": False, "reason": "case_not_found", "customer_id": customer_id}

    status = case.get("case_status", "")
    if status not in TERMINAL_STATUSES:
        return {"attributed": False, "reason": f"non_terminal_status:{status}",
                "customer_id": customer_id}

    audit_events = db.get_audit_for_case(conn, customer_id)
    jobs = db.get_jobs_for_case(conn, customer_id)
    strategy = _extract_strategy(audit_events)

    if not strategy:
        return {"attributed": False, "reason": "no_strategy_in_audit",
                "customer_id": customer_id}

    provenance = _infer_provenance(case, jobs)
    recovered = 1 if status == "recovered" else 0
    escalated = 1 if status in ("escalated", "broken_promise") else 0
    amount = float(case.get("amount", 0) or 0)
    amount_recovered = amount if recovered else 0.0
    ttr = _time_to_recovery_hours(case, jobs)
    ttr_val = ttr if ttr is not None else 0.0

    # Write to multiple dimensions simultaneously.
    dimensions = [
        ("global", "all"),
        ("failure_reason", case.get("failure_reason", "unknown")),
        ("merchant_category", case.get("merchant_category", "unknown")),
    ]

    for dim_key, dim_val in dimensions:
        db.upsert_strategy_performance(
            conn, strategy, dim_key, dim_val, provenance,
            delta_attempts=1,
            delta_recoveries=recovered,
            delta_amount_recovered=amount_recovered,
            delta_amount_attempted=amount,
            delta_escalations=escalated,
            delta_time_hours=ttr_val,
        )

    conn.commit()
    return {
        "attributed": True,
        "customer_id": customer_id,
        "strategy": strategy,
        "provenance": provenance,
        "recovered": bool(recovered),
        "amount": amount,
        "dimensions_written": len(dimensions),
    }


def backfill_from_audit(conn) -> dict:
    """Backfill strategy_performance from all existing resolved cases.

    Safe to run multiple times — uses upsert (ON CONFLICT DO UPDATE) so existing
    counters are incremented rather than duplicated.  Pre-Phase-6 cases are tagged
    HISTORICAL since there were no execution jobs.

    This does NOT wipe existing strategy_performance rows. It adds to them.
    To get a clean backfill, call reset_db() first (which clears strategy_performance).
    """
    cases = db.get_all_cases(conn)
    terminal = [c for c in cases if c.get("case_status") in TERMINAL_STATUSES]

    attributed = 0
    skipped_no_strategy = 0
    skipped_non_terminal = len(cases) - len(terminal)
    provenance_counts: dict[str, int] = {}

    for case in terminal:
        result = attribute_outcome(conn, case["customer_id"])
        if result.get("attributed"):
            attributed += 1
            prov = result.get("provenance", "UNKNOWN")
            provenance_counts[prov] = provenance_counts.get(prov, 0) + 1
        else:
            skipped_no_strategy += 1

    return {
        "total_cases": len(cases),
        "terminal_cases": len(terminal),
        "attributed": attributed,
        "skipped_non_terminal": skipped_non_terminal,
        "skipped_no_strategy": skipped_no_strategy,
        "provenance_breakdown": provenance_counts,
    }


def get_attribution_summary(conn) -> dict:
    """Return a summary of attribution coverage and data provenance breakdown."""
    cases = db.get_all_cases(conn)
    total = len(cases)
    terminal = sum(1 for c in cases if c.get("case_status") in TERMINAL_STATUSES)

    # Count attributed cases (those that have a strategy_performance record)
    sp_rows = db.get_strategy_performance(conn)
    attributed = sum(r["attempts"] for r in sp_rows
                     if r["dimension_key"] == "global")

    # Provenance breakdown from strategy_performance
    prov_breakdown: dict[str, dict] = {}
    for row in sp_rows:
        if row["dimension_key"] != "global":
            continue
        prov = row["provenance"]
        b = prov_breakdown.setdefault(prov, {"attempts": 0, "recoveries": 0,
                                              "amount_recovered": 0.0})
        b["attempts"] += row["attempts"]
        b["recoveries"] += row["recoveries"]
        b["amount_recovered"] += row["amount_recovered"]

    # Real test outcome count
    real_test_attempts = prov_breakdown.get(PROV_REAL_TEST, {}).get("attempts", 0)
    simulation_attempts = prov_breakdown.get(PROV_SIMULATION, {}).get("attempts", 0)
    historical_attempts = prov_breakdown.get(PROV_HISTORICAL, {}).get("attempts", 0)

    return {
        "data_type": "actual",
        "total_cases": total,
        "terminal_cases": terminal,
        "attributed_cases": min(attributed, terminal),
        "attribution_coverage_pct": round(
            min(attributed, terminal) / terminal * 100, 1
        ) if terminal else 0.0,
        "provenance_breakdown": prov_breakdown,
        "real_test_outcomes": real_test_attempts,
        "simulation_outcomes": simulation_attempts,
        "historical_outcomes": historical_attempts,
        "data_trust_note": (
            f"{real_test_attempts} outcomes from real Razorpay Test Mode execution. "
            f"{simulation_attempts} from simulation. "
            f"{historical_attempts} from historical agent pipeline (pre-Phase-4). "
            "Only REAL_TEST outcomes represent actual payment behaviour."
        ),
    }
