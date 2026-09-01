"""Baseline simulations (design.md section 9, R11).

Two baselines, both using the SAME seeded cases and the SAME success-probability
model as the real agent (`agent._success_prob`), so every comparison is apples-to-
apples on the probability model — only the STRATEGY differs. Neither baseline
writes to audit_log or mutates case_status; both are pure, side-effect-free reads
used only for the dashboard comparison.

1. `run_baseline()` — the NAIVE baseline: one attempt per case, no scoring, no
   salary-window timing, no promise flow, no dunning. Models "the merchant does
   nothing smart at all."

2. `run_dumb_persistence_baseline()` — a second, tougher baseline: the SAME hard
   3-retry cap as the real agent, but with NO scoring, NO salary-window timing, NO
   promise-to-pay flow, and NO dunning nudges — just blind repetition. This isolates
   the value of "trying more times" from the value of the agent's actual
   intelligence (scoring, timing, staged dunning, promise handling). Without this
   second baseline, "the agent recovers more than doing nothing once" partly just
   proves that 3 attempts beats 1 attempt, which is a weaker claim than "the
   intelligence layer itself adds recovery beyond persistence alone."

Comparing agent vs. dumb-persistence vs. naive gives three honestly distinct
numbers:
  naive (1 try)  <  dumb persistence (3 tries, no strategy)  <=?  agent (3 tries + strategy)
The agent-vs-dumb-persistence delta is the real, defensible measure of what the
scoring/strategy/timing/dunning layer is worth, independent of retry count.
"""

import random

import db
from agent import _success_prob, MAX_RETRIES
import scoring

BASELINE_SEED = 42
DUMB_PERSISTENCE_SEED = 43  # distinct stream so it's not perfectly correlated with the naive baseline


def run_baseline(conn=None):
    """NAIVE baseline: single attempt per case, including revoked mandates.

    Returns {'amount_recovered', 'recovered_cases', 'recovery_rate', 'total_cases'}.
    """
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


def run_dumb_persistence_baseline(conn=None, retry_cap=None):
    """"Dumb persistence" baseline: same retry BUDGET as the real agent (default 3
    attempts), but no scoring, no timing, no dunning, no promise-to-pay — just blind
    repeated attempts at a flat per-attempt probability for every case, including
    revoked mandates (a naive merchant would not know a mandate was unrecoverable).

    This isolates "value of trying more times" from "value of the agent's actual
    strategy", so the agent-vs-this-baseline delta is a defensible measure of what
    the intelligence layer specifically contributes.

    Returns the same shape as run_baseline() plus 'retry_cap' and 'attempts_used'.
    """
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        cap = int(retry_cap) if retry_cap else MAX_RETRIES
        cases = db.get_all_cases(conn)
        rng = random.Random(DUMB_PERSISTENCE_SEED)
        total = len(cases)
        recovered = 0
        amount_recovered = 0.0
        amount_at_risk = 0.0
        attempts_used = 0
        for case in cases:
            amount_at_risk += float(case["amount"])
            score, _ = scoring.score_case(case)
            prob = _success_prob(case, score)
            case_recovered = False
            for _attempt in range(cap):
                attempts_used += 1
                if rng.random() < prob:
                    case_recovered = True
                    break
            if case_recovered:
                recovered += 1
                amount_recovered += float(case["amount"])
        return {
            "total_cases": total,
            "amount_at_risk": round(amount_at_risk, 2),
            "amount_recovered": round(amount_recovered, 2),
            "recovered_cases": recovered,
            "recovery_rate": round(recovered / total, 4) if total else 0.0,
            "retry_cap": cap,
            "attempts_used": attempts_used,
        }
    finally:
        if own:
            conn.close()
