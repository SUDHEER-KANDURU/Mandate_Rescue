"""Recovery agent state machine (design.md section 7).

Processes cases in descending recoverability-score order (R6 triage). Applies a
distinct strategy per failure_reason (R1), enforces a hard 3-retry cap (R2), runs a
promise-to-pay sub-flow with a broken-promise path (R3), and writes an audit_log row
for every transition including failures (R4). Outcomes are drawn from a seeded RNG so
runs are reproducible (N4) while recovered numbers stay emergent, not hardcoded (N1).

Note: R13-R16 storage fields exist on the case, but their *behavior* (webhook logging,
mandate-limit gate, RBI compliance, staged dunning) is implemented in Phase 3. This
module implements only tasks 1-9 scope.
"""

import random
from datetime import datetime, timedelta

import db
import scoring
import salary_window
import messaging

MAX_RETRIES = 3
RUN_SEED = 42

# Base per-attempt success probability by failure_reason. Blended with the case's
# recoverability score so stronger cases are likelier to recover.
BASE_SUCCESS_PROB = {
    "insufficient_funds": 0.45,
    "mandate_expired": 0.40,
    "bank_technical_error": 0.75,
    "mandate_revoked": 0.0,
}

# Probability that a customer records a promise-to-pay when nudged, and that they
# then keep it. Tuned for a realistic mix of kept / broken promises.
PROMISE_OFFER_PROB = 0.30
PROMISE_KEPT_PROB = 0.55


def _now_iso(offset_seconds=0):
    return (datetime.now() + timedelta(seconds=offset_seconds)).isoformat(timespec="seconds")


def _success_prob(case, score):
    """Blend base per-reason rate with recoverability score (0-100)."""
    base = BASE_SUCCESS_PROB.get(case.get("failure_reason", ""), 0.3)
    return max(0.0, min(1.0, base + (score / 100.0 - 0.5) * 0.3))


class AgentRun:
    """Holds per-run state: a DB connection, a seeded RNG, and an event clock."""

    def __init__(self, conn, rng):
        self.conn = conn
        self.rng = rng
        self._tick = 0

    def _ts(self):
        """Monotonic timestamp so audit rows keep insertion order within a case."""
        self._tick += 1
        return _now_iso(self._tick)

    def log(self, case, event_type, action, outcome, attempt, reasoning, status_after):
        db.insert_audit(self.conn, case["customer_id"], self._ts(), event_type,
                        action, outcome, attempt, reasoning, status_after)

    def set_status(self, case, status):
        case["case_status"] = status
        db.update_case(self.conn, case["customer_id"], case_status=status)


    def attempt_retry(self, case, score, attempt, event_type, action_label, window=None):
        """Simulate one debit attempt. Returns True on success. Logs the attempt."""
        prob = _success_prob(case, score)
        success = self.rng.random() < prob
        win_txt = ""
        if window is not None:
            win_txt = f" during {window['label']} window (days {window['window'][0]}-{window['window'][1]})"
        if success:
            reasoning = (
                f"Attempt {attempt} of {MAX_RETRIES}: retried{win_txt}; "
                f"succeeded (est. {int(prob * 100)}% based on score {score})."
            )
            self.log(case, event_type, action_label, "success", attempt, reasoning, "recovered")
            self.set_status(case, "recovered")
            return True
        reasoning = (
            f"Attempt {attempt} of {MAX_RETRIES}: retried{win_txt}; "
            f"failed (est. {int(prob * 100)}% based on score {score})."
        )
        self.log(case, event_type, action_label, "failure", attempt, reasoning, "in_progress")
        return False

    def escalate(self, case, attempt, reason):
        self.log(case, "escalate", "Escalated to manual recovery", "n/a", attempt, reason, "escalated")
        self.set_status(case, "escalated")
