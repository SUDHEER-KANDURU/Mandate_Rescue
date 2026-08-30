"""Naive baseline simulation (design.md section 9, R11).

Models the naive merchant approach: one retry per case with a single generic message,
no scoring, no salary-window timing, no promise flow. Uses the same seeded cases and
the same success-probability model as the agent so the comparison is apples-to-apples.

The baseline does NOT write to audit_log or mutate case_status; it is a pure,
side-effect-free computation used only for the dashboard comparison card.
"""

import random

import db
from agent import _success_prob
import scoring

BASELINE_SEED = 42


def run_baseline(conn=None):
    """Return {'amount_recovered', 'recovered_cases', 'recovery_rate', 'total_cases'}."""
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        cases = db.get_all_cases(conn)
        rng = random.Random(BASELINE_SEED)
        total = len(cases)
        recovered = 0
        amount_recovered = 0.0
        amount_at_risk = 0.0
        for case in cases:
            amount_at_risk += float(case["amount"])
            # Naive: single attempt for everyone, including revoked mandates.
            score, _ = scoring.score_case(case)
            prob = _success_prob(case, score)
            if rng.random() < prob:
                recovered += 1
                amount_recovered += float(case["amount"])
        return {
            "total_cases": total,
            "amount_at_risk": round(amount_at_risk, 2),
            "amount_recovered": round(amount_recovered, 2),
            "recovered_cases": recovered,
            "recovery_rate": round(recovered / total, 4) if total else 0.0,
        }
    finally:
        if own:
            conn.close()
