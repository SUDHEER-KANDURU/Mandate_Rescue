"""Experiment Evaluation Engine — Phase 6.

Computes statistically honest results for completed (or in-progress) experiments.

What it produces
----------------
For each arm:
  - sample_size
  - recoveries
  - recovery_rate
  - amount_recovered
  - escalation_rate
  - avg_time_to_recovery_hours

Difference (treatment − control):
  - recovery_rate_diff
  - amount_diff
  - estimated_incremental_revenue

Confidence / uncertainty:
  - Uses a two-proportion z-test when both arms have >= MIN_Z_SAMPLE observations.
  - Falls back to "insufficient data" when sample is too small.
  - Clearly distinguishes "observed result" from "estimated result".

Counterfactual:
  - Uses the existing economic_value incremental_value() model to estimate what
    baseline (naive 1 attempt) would have recovered vs the control strategy.
  - Clearly labelled ESTIMATE — counterfactual, not causal proof.

Data provenance:
  - Every result carries execution_mode counts so the reader knows how many
    observations come from real Razorpay Test Mode vs simulation.
  - Simulation-only experiments are marked data_type: "simulation".
  - Mixed experiments carry a warning.

Public API
----------
evaluate_experiment(conn, experiment_id) → dict
"""

from __future__ import annotations

import json
import math
from typing import Optional

import db
from experimentation import MIN_ARM_SAMPLE

# Minimum observations per arm to apply a z-test.
MIN_Z_SAMPLE = 30

# Minimum absolute recovery-rate difference to call a result "meaningful".
MIN_MEANINGFUL_DIFF = 0.03  # 3 percentage points

# Monthly periods to project incremental revenue (12 = 1 year horizon).
PROJECTION_MONTHS = 1


def _arm_stats(outcomes: list[dict]) -> dict:
    """Compute per-arm statistics from experiment_outcomes rows."""
    n = len(outcomes)
    if n == 0:
        return {
            "sample_size": 0, "recoveries": 0, "recovery_rate": 0.0,
            "amount_recovered": 0.0, "amount_attempted": 0.0,
            "escalations": 0, "escalation_rate": 0.0,
            "avg_time_to_recovery_hours": None,
            "real_test_count": 0, "simulation_count": 0,
        }

    recoveries = sum(o["recovered"] for o in outcomes)
    amount_recovered = sum(
        float(o["amount_rupees"]) for o in outcomes if o["recovered"]
    )
    amount_attempted = sum(float(o["amount_rupees"]) for o in outcomes)
    escalations = sum(
        1 for o in outcomes if o["outcome_status"] in ("escalated", "broken_promise")
    )
    ttrs = [
        o["time_to_recovery_hours"]
        for o in outcomes
        if o.get("time_to_recovery_hours") is not None
    ]
    avg_ttr = round(sum(ttrs) / len(ttrs), 2) if ttrs else None

    real_test = sum(1 for o in outcomes if o.get("execution_mode") == "real_test")
    simulation = sum(1 for o in outcomes if o.get("execution_mode") != "real_test")

    return {
        "sample_size": n,
        "recoveries": recoveries,
        "recovery_rate": round(recoveries / n, 4) if n else 0.0,
        "amount_recovered": round(amount_recovered, 2),
        "amount_attempted": round(amount_attempted, 2),
        "escalations": escalations,
        "escalation_rate": round(escalations / n, 4) if n else 0.0,
        "avg_time_to_recovery_hours": avg_ttr,
        "real_test_count": real_test,
        "simulation_count": simulation,
    }


def _two_proportion_z(p1: float, n1: int, p2: float, n2: int) -> Optional[float]:
    """Two-proportion z-test: H0: p1 == p2. Returns z-score or None if not applicable."""
    if n1 < MIN_Z_SAMPLE or n2 < MIN_Z_SAMPLE:
        return None
    p_pool = (p1 * n1 + p2 * n2) / (n1 + n2)
    if p_pool <= 0 or p_pool >= 1:
        return None
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    if se == 0:
        return None
    return (p2 - p1) / se


def _confidence_label(z: Optional[float], diff: float, n_control: int, n_treatment: int) -> str:
    """Return a human-readable confidence label based on sample size and z-score."""
    if n_control < MIN_ARM_SAMPLE or n_treatment < MIN_ARM_SAMPLE:
        return "insufficient_data"
    if z is None:
        # Small sample: use relative size as proxy
        min_n = min(n_control, n_treatment)
        if min_n < 20:
            return "very_low"
        if min_n < MIN_Z_SAMPLE:
            return "low"
        return "low"
    abs_z = abs(z)
    if abs_z >= 2.576:  # p < 0.01
        return "high"
    if abs_z >= 1.96:   # p < 0.05
        return "moderate"
    if abs_z >= 1.282:  # p < 0.20
        return "low"
    return "very_low"


def _incremental_revenue_estimate(
    control_stats: dict, treatment_stats: dict
) -> dict:
    """Estimate the incremental revenue attributable to the treatment strategy.

    Uses the observed recovery-rate difference × total amount attempted in the
    treatment arm. This is an ESTIMATE — not a causal proof. Clearly labelled.

    Per-month projection = per-experiment-period amount × (30 / experiment_days).
    We do not assume experiment_days here; the caller may pass actual days.
    """
    diff_rate = treatment_stats["recovery_rate"] - control_stats["recovery_rate"]
    treatment_amount = treatment_stats["amount_attempted"]
    incremental_amount = round(diff_rate * treatment_amount, 2)

    return {
        "data_type": "estimate",
        "observed_diff_recovery_rate": round(diff_rate, 4),
        "treatment_amount_attempted_rs": treatment_amount,
        "estimated_incremental_rs": incremental_amount,
        "note": (
            "Incremental revenue is estimated as (rate_diff × treatment_amount_attempted). "
            "This is NOT a causal proof — a positive rate difference in a single experiment "
            "does not guarantee equivalent results at scale. [ESTIMATE]"
        ),
    }


def evaluate_experiment(conn, experiment_id: str) -> dict:
    """Evaluate an experiment and return a full results dict.

    Works for both in-progress and completed experiments. In-progress results
    are marked 'preliminary' and may change as more outcomes are recorded.

    Returns a self-contained dict with:
      - experiment metadata
      - control_arm stats
      - treatment_arm stats
      - difference
      - confidence / uncertainty
      - incremental_revenue (estimate)
      - data provenance
      - insufficient_data flag with explanation when sample is too small
    """
    experiment = db.get_experiment(conn, experiment_id)
    if not experiment:
        return {"error": f"Experiment {experiment_id} not found"}

    outcomes = db.get_experiment_outcomes(conn, experiment_id)
    control_outcomes = [o for o in outcomes if o["arm"] == "control"]
    treatment_outcomes = [o for o in outcomes if o["arm"] == "treatment"]

    control = _arm_stats(control_outcomes)
    treatment = _arm_stats(treatment_outcomes)

    n_c = control["sample_size"]
    n_t = treatment["sample_size"]
    min_arm = experiment.get("min_sample_size") or MIN_ARM_SAMPLE

    insufficient = n_c < min_arm or n_t < min_arm
    if insufficient:
        return {
            "experiment_id": experiment_id,
            "name": experiment["name"],
            "status": experiment["status"],
            "control_strategy": experiment["control_strategy"],
            "treatment_strategy": experiment["treatment_strategy"],
            "control_arm": control,
            "treatment_arm": treatment,
            "sufficient_data": False,
            "insufficient_data_explanation": (
                f"Insufficient data for a meaningful result. "
                f"Control arm: {n_c} observations (need ≥ {min_arm}). "
                f"Treatment arm: {n_t} observations (need ≥ {min_arm}). "
                f"Collect more outcomes before drawing conclusions."
            ),
            "required_per_arm": min_arm,
            "data_type": _overall_data_type(control_outcomes + treatment_outcomes),
            "is_preliminary": experiment["status"] != "completed",
        }

    rate_diff = round(treatment["recovery_rate"] - control["recovery_rate"], 4)
    amount_diff = round(treatment["amount_recovered"] - control["amount_recovered"], 2)
    z = _two_proportion_z(
        control["recovery_rate"], n_c,
        treatment["recovery_rate"], n_t,
    )
    confidence = _confidence_label(z, rate_diff, n_c, n_t)
    incremental = _incremental_revenue_estimate(control, treatment)
    data_type = _overall_data_type(control_outcomes + treatment_outcomes)

    # Is the treatment meaningfully better / worse / neutral?
    if abs(rate_diff) < MIN_MEANINGFUL_DIFF:
        verdict = "no_meaningful_difference"
    elif rate_diff > 0:
        verdict = "treatment_better"
    else:
        verdict = "control_better"

    return {
        "experiment_id": experiment_id,
        "name": experiment["name"],
        "description": experiment.get("description", ""),
        "status": experiment["status"],
        "control_strategy": experiment["control_strategy"],
        "treatment_strategy": experiment["treatment_strategy"],
        "cohort": json.loads(experiment.get("cohort_definition", "{}")),
        "created_at": experiment["created_at"],
        "ended_at": experiment.get("ended_at"),
        "control_arm": control,
        "treatment_arm": treatment,
        "difference": {
            "recovery_rate_diff": rate_diff,
            "recovery_rate_diff_pct": f"{rate_diff * 100:+.1f}pp",
            "amount_diff_rs": amount_diff,
            "z_score": round(z, 3) if z is not None else None,
            "confidence": confidence,
            "verdict": verdict,
        },
        "incremental_revenue": incremental,
        "sufficient_data": True,
        "is_preliminary": experiment["status"] != "completed",
        "data_type": data_type,
        "data_type_note": _data_type_note(control_outcomes + treatment_outcomes),
    }


def _overall_data_type(outcomes: list[dict]) -> str:
    """Determine overall data provenance label for a set of outcomes."""
    if not outcomes:
        return "no_data"
    real = sum(1 for o in outcomes if o.get("execution_mode") == "real_test")
    sim = len(outcomes) - real
    if real > 0 and sim > 0:
        return "mixed"
    if real > 0:
        return "real_test"
    return "simulation"


def _data_type_note(outcomes: list[dict]) -> str:
    real = sum(1 for o in outcomes if o.get("execution_mode") == "real_test")
    sim = len(outcomes) - real
    if real > 0 and sim == 0:
        return (
            f"All {real} observations are from Razorpay Test Mode execution "
            "(REAL_TEST). These represent actual payment processing behaviour."
        )
    if real == 0:
        return (
            f"All {sim} observations are from simulation (SIMULATION). "
            "Results indicate expected behaviour under the probability model "
            "but do NOT represent real payment outcomes."
        )
    return (
        f"{real} observations from Razorpay Test Mode (REAL_TEST) and "
        f"{sim} from simulation (SIMULATION). Mixed evidence — "
        "do not treat simulation outcomes as equivalent to real test outcomes."
    )
