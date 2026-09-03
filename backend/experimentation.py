"""Controlled Strategy Experimentation — Phase 6.

Implements A/B experiment lifecycle:
  CREATE → ASSIGN → OBSERVE → COMPLETE → EVALUATE

Design principles
-----------------
- Experiments compare CONTROL vs TREATMENT strategy for a defined cohort.
- Each case is assigned to exactly one arm once (idempotent via UNIQUE constraint).
- Assignment uses deterministic hashing so the same case always lands in the same
  arm even if assign_cases() is called multiple times.
- Outcomes are recorded once per case (immutable after recording).
- No winner is declared until MIN_SAMPLE_SIZE is reached in BOTH arms.
- Synthetic/simulation experiments are labelled SIMULATION; real Razorpay Test
  Mode experiments are labelled REAL_TEST. These must never be combined silently.

Public API
----------
create_experiment(conn, ...) → experiment_id (str)
assign_cases(conn, experiment_id) → {assigned, skipped, total}
record_case_outcome(conn, customer_id) → {recorded, reason}
complete_experiment(conn, experiment_id) → {completed, outcomes_recorded}
get_experiment_status(conn, experiment_id) → dict
list_experiments(conn) → list[dict]
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

import db
from outcome_attribution import (
    PROV_REAL_TEST, PROV_SIMULATION, PROV_HISTORICAL,
    _extract_strategy, _infer_provenance, _time_to_recovery_hours,
    TERMINAL_STATUSES,
)

# Minimum number of observations in EACH arm before the experiment can surface
# a meaningful result. Below this threshold we show "insufficient data".
MIN_ARM_SAMPLE = 10


def _arm_for_case(experiment_id: str, customer_id: str) -> str:
    """Deterministic arm assignment: hash(experiment_id + customer_id) mod 2.

    Returns 'control' or 'treatment'. Using a hash keeps assignment stable across
    multiple calls to assign_cases() without persisting a random coin flip.
    """
    seed = f"{experiment_id}:{customer_id}"
    digest = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return "control" if digest % 2 == 0 else "treatment"


def create_experiment(
    conn,
    name: str,
    control_strategy: str,
    treatment_strategy: str,
    description: str = "",
    merchant_category: Optional[str] = None,
    failure_reason: Optional[str] = None,
    min_sample_size: int = MIN_ARM_SAMPLE,
    created_by: str = "system",
    notes: str = None,
) -> str:
    """Create a new experiment. Returns the new experiment_id."""
    experiment_id = str(uuid.uuid4())
    cohort = json.dumps({
        "merchant_category": merchant_category,
        "failure_reason": failure_reason,
    })
    ok = db.create_experiment(
        conn,
        experiment_id=experiment_id,
        name=name,
        description=description,
        control_strategy=control_strategy,
        treatment_strategy=treatment_strategy,
        cohort_definition=cohort,
        merchant_category=merchant_category,
        failure_reason=failure_reason,
        min_sample_size=min_sample_size,
        created_by=created_by,
    )
    if not ok:
        raise RuntimeError(f"Failed to create experiment '{name}'")
    conn.commit()
    return experiment_id


def _case_matches_cohort(case: dict, experiment: dict) -> bool:
    """Return True if this case belongs to the experiment's cohort."""
    if experiment.get("merchant_category"):
        if case.get("merchant_category") != experiment["merchant_category"]:
            return False
    if experiment.get("failure_reason"):
        if case.get("failure_reason") != experiment["failure_reason"]:
            return False
    # Exclude terminal cases — they've already resolved, can't be assigned
    if case.get("case_status") in TERMINAL_STATUSES:
        return False
    # Exclude rejected/invalid
    if case.get("case_status") in ("rejected", "invalid", "duplicate"):
        return False
    return True


def assign_cases(conn, experiment_id: str) -> dict:
    """Assign all eligible cases to control/treatment arms.

    Idempotent: already-assigned cases are skipped (UNIQUE constraint).
    Returns {assigned, skipped_already_assigned, skipped_ineligible, total_eligible}.
    """
    experiment = db.get_experiment(conn, experiment_id)
    if not experiment:
        return {"error": f"Experiment {experiment_id} not found"}
    if experiment["status"] != "active":
        return {"error": f"Experiment {experiment_id} is not active (status={experiment['status']})"}

    cases = db.get_all_cases(conn)
    assigned = 0
    skipped_assigned = 0
    skipped_ineligible = 0

    for case in cases:
        if not _case_matches_cohort(case, experiment):
            skipped_ineligible += 1
            continue
        arm = _arm_for_case(experiment_id, case["customer_id"])
        ok = db.assign_experiment_case(conn, experiment_id, case["customer_id"], arm)
        if ok:
            assigned += 1
        else:
            skipped_assigned += 1

    conn.commit()
    return {
        "experiment_id": experiment_id,
        "assigned": assigned,
        "skipped_already_assigned": skipped_assigned,
        "skipped_ineligible": skipped_ineligible,
        "total_eligible": assigned + skipped_assigned,
    }


def record_case_outcome(conn, customer_id: str) -> dict:
    """Record the final outcome for a case in any active experiment it belongs to.

    Should be called whenever a case reaches a terminal status.
    Safe to call multiple times — UNIQUE constraint prevents duplicates.
    Returns {recorded, experiment_id, arm, reason}.
    """
    assignment = db.get_case_experiment_arm(conn, customer_id)
    if not assignment:
        return {"recorded": False, "reason": "no_active_experiment_assignment",
                "customer_id": customer_id}

    case = db.get_case(conn, customer_id)
    if not case:
        return {"recorded": False, "reason": "case_not_found", "customer_id": customer_id}

    status = case.get("case_status", "")
    if status not in TERMINAL_STATUSES:
        return {"recorded": False, "reason": f"non_terminal:{status}",
                "customer_id": customer_id}

    audit_events = db.get_audit_for_case(conn, customer_id)
    jobs = db.get_jobs_for_case(conn, customer_id)
    strategy_used = _extract_strategy(audit_events) or assignment.get("arm") + "_default"
    provenance = _infer_provenance(case, jobs)
    execution_mode = "real_test" if provenance == PROV_REAL_TEST else "simulation"
    recovered = 1 if status == "recovered" else 0
    amount = float(case.get("amount", 0) or 0)
    ttr = _time_to_recovery_hours(case, jobs)

    ok = db.record_experiment_outcome(
        conn,
        experiment_id=assignment["experiment_id"],
        customer_id=customer_id,
        arm=assignment["arm"],
        strategy_used=strategy_used,
        outcome_status=status,
        amount_rupees=amount,
        recovered=recovered,
        time_to_recovery_hours=ttr,
        execution_mode=execution_mode,
    )
    if ok:
        conn.commit()
    return {
        "recorded": ok,
        "reason": "already_recorded" if not ok else "success",
        "customer_id": customer_id,
        "experiment_id": assignment["experiment_id"],
        "arm": assignment["arm"],
        "provenance": provenance,
    }


def record_all_terminal_outcomes(conn, experiment_id: str) -> dict:
    """Sweep all assigned cases and record outcomes for any that are now terminal."""
    assignments = db.get_experiment_assignments(conn, experiment_id)
    recorded = 0
    skipped = 0
    for a in assignments:
        result = record_case_outcome(conn, a["customer_id"])
        if result.get("recorded"):
            recorded += 1
        else:
            skipped += 1
    return {"recorded": recorded, "skipped": skipped}


def complete_experiment(conn, experiment_id: str) -> dict:
    """Mark an experiment completed; records all remaining terminal outcomes first."""
    experiment = db.get_experiment(conn, experiment_id)
    if not experiment:
        return {"completed": False, "reason": "not_found"}
    if experiment["status"] not in ("active", "paused"):
        return {"completed": False, "reason": f"already_{experiment['status']}"}

    sweep = record_all_terminal_outcomes(conn, experiment_id)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.update_experiment_status(conn, experiment_id, "completed", ended_at=now)
    conn.commit()
    return {
        "completed": True,
        "experiment_id": experiment_id,
        "outcomes_recorded_this_sweep": sweep["recorded"],
    }


def get_experiment_status(conn, experiment_id: str) -> dict:
    """Return full experiment status including arm sample sizes."""
    experiment = db.get_experiment(conn, experiment_id)
    if not experiment:
        return {"error": f"Experiment {experiment_id} not found"}

    assignments = db.get_experiment_assignments(conn, experiment_id)
    outcomes = db.get_experiment_outcomes(conn, experiment_id)

    control_assigned = sum(1 for a in assignments if a["arm"] == "control")
    treatment_assigned = sum(1 for a in assignments if a["arm"] == "treatment")
    control_outcomes = [o for o in outcomes if o["arm"] == "control"]
    treatment_outcomes = [o for o in outcomes if o["arm"] == "treatment"]

    min_arm = experiment.get("min_sample_size") or MIN_ARM_SAMPLE
    sufficient = (
        len(control_outcomes) >= min_arm and
        len(treatment_outcomes) >= min_arm
    )

    provenance_set: set[str] = set()
    for o in outcomes:
        provenance_set.add(o.get("execution_mode", "simulation"))

    return {
        "experiment_id": experiment_id,
        "name": experiment["name"],
        "description": experiment.get("description", ""),
        "status": experiment["status"],
        "control_strategy": experiment["control_strategy"],
        "treatment_strategy": experiment["treatment_strategy"],
        "cohort": json.loads(experiment.get("cohort_definition", "{}")),
        "created_at": experiment["created_at"],
        "started_at": experiment.get("started_at"),
        "ended_at": experiment.get("ended_at"),
        "min_sample_size": min_arm,
        "control_assigned": control_assigned,
        "treatment_assigned": treatment_assigned,
        "control_outcomes_recorded": len(control_outcomes),
        "treatment_outcomes_recorded": len(treatment_outcomes),
        "sufficient_for_evaluation": sufficient,
        "execution_modes": sorted(provenance_set),
    }


def list_experiments(conn) -> list[dict]:
    """Return all experiments with summary counts."""
    exps = db.get_all_experiments(conn)
    result = []
    for exp in exps:
        status_info = get_experiment_status(conn, exp["experiment_id"])
        result.append(status_info)
    return result
