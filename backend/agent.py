"""Recovery agent pipeline (design.md section 7).

The recovery flow is organized as an explicit four-agent pipeline. Each stage is a
small class with a single `process(...)` method, so the orchestration reads as:

    DiagnosisAgent -> TriageAgent -> StrategyAgent -> CommunicationAgent

This is an architectural clarification only. The decision behavior is byte-for-byte
identical to the original single-class implementation: the same scoring, the same
hard 3-retry cap, the same RBI pre-debit compliance gate, the same staged dunning,
and the same seeded RNG draw order (so recovered/escalated counts never change).

Stage responsibilities:
- DiagnosisAgent: classify the incoming Razorpay-style webhook event into a
  failure_reason + raw_event_type and log the `webhook_received` event (R13).
- TriageAgent: compute the recoverability score (R6) and the subscription health
  score (R17), and decide processing order (highest score first).
- StrategyAgent: apply the per-reason strategy (R1), the UPI mandate-limit gate
  (R14), the hard retry cap (R2), the promise-to-pay sub-flow (R3), and the RBI
  pre-debit notification compliance check (R15). Writes an audit row per transition
  (R4). This stage owns every decision; it never invents facts.
- CommunicationAgent: generate the LLM-narrated reasoning text and the customer
  nudge messages, and manage the Day 1 / Day 3 / Day 7 dunning stage sequence (R16,
  R9). Narration only — it never changes a decision.

Because dunning nudges are interleaved between retry attempts, StrategyAgent holds a
CommunicationAgent collaborator and calls it at the right moments, rather than the
CommunicationAgent running as a separate trailing pass. The pipeline order above is
still the true data-flow order for each case.
"""

import random
from datetime import datetime, timedelta

import db
import scoring
import salary_window
import messaging
import llm_client
import health as health_module
import webhook_security

MAX_RETRIES = 3
RUN_SEED = 42

# Cost/latency control for live runs: generate LLM narration/messages live for only
# the top-N highest-value cases; the rest use deterministic templates. This keeps a
# full 180-case run inside the demo's latency budget against a real remote LLM.
# Set via env LLM_LIVE_TOP_N (0 = no cap: every case may use the LLM). See README.
import os as _os
LLM_LIVE_TOP_N = int(_os.environ.get("LLM_LIVE_TOP_N", "20"))

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

# R15: RBI mandates a pre-debit notification at least 24h before an auto-debit retry.
RBI_MIN_NOTICE_HOURS = 24

# R16: 3-stage dunning sequence. Each stage maps to a day offset and a tone.
DUNNING_STAGES = [
    {"stage": 1, "day": 1, "tone": "friendly reminder"},
    {"stage": 2, "day": 3, "tone": "firmer follow-up"},
    {"stage": 3, "day": 7, "tone": "final notice before escalation"},
]

# Failure reasons whose recovery involves customer-facing nudges (dunning applies).
NUDGE_REASONS = ("insufficient_funds", "mandate_expired")

# Human-readable strategy label per failure_reason. Used only to give the LLM
# narration a name for the (already rule-decided) strategy; it does not affect logic.
_STRATEGY_LABELS = {
    "insufficient_funds": "salary-window retry",
    "mandate_expired": "re-authorization link then retry",
    "bank_technical_error": "silent quick retry",
    "mandate_revoked": "immediate escalation (no retry permitted)",
}

# R13: map a Razorpay-style raw webhook event onto our internal failure_reason. The
# seed already provides both fields; DiagnosisAgent uses this table to (a) fill in a
# missing raw_event_type and (b) classify a raw event when failure_reason is absent.
WEBHOOK_TO_REASON = {
    "payment.failed": "insufficient_funds",
    "subscription.charged.failed": "insufficient_funds",
    "mandate.expired": "mandate_expired",
    "mandate.revoked": "mandate_revoked",
    "mandate.paused": "mandate_revoked",
    "payment.dispute.created": "bank_technical_error",
}
REASON_TO_WEBHOOK = {
    "insufficient_funds": "payment.failed",
    "mandate_expired": "mandate.expired",
    "mandate_revoked": "mandate.revoked",
    "bank_technical_error": "payment.failed",
}


def _now_iso(offset_seconds=0):
    return (datetime.now() + timedelta(seconds=offset_seconds)).isoformat(timespec="seconds")


def _success_prob(case, score):
    """Blend base per-reason rate with recoverability score (0-100).

    Kept as a module-level function (not moved into a class) because baseline.py
    imports it directly to run the apples-to-apples naive comparison.
    """
    base = BASE_SUCCESS_PROB.get(case.get("failure_reason", ""), 0.3)
    return max(0.0, min(1.0, base + (score / 100.0 - 0.5) * 0.3))


class _RunContext:
    """Shared per-run state: a DB connection, a seeded RNG, and an event clock.

    All four agents share one context so audit rows keep a monotonic order and the
    RNG draw sequence stays identical to the original implementation.
    """

    def __init__(self, conn, rng):
        self.conn = conn
        self.rng = rng
        self._tick = 0

    def ts(self):
        """Monotonic timestamp so audit rows keep insertion order within a case."""
        self._tick += 1
        return _now_iso(self._tick)

    def log(self, case, event_type, action, outcome, attempt, reasoning, status_after):
        db.insert_audit(self.conn, case["customer_id"], self.ts(), event_type,
                        action, outcome, attempt, reasoning, status_after)

    def set_status(self, case, status):
        case["case_status"] = status
        db.update_case(self.conn, case["customer_id"], case_status=status)


# ---------------------------------------------------------------------------
# Stage 1: DiagnosisAgent
# ---------------------------------------------------------------------------
class DiagnosisAgent:
    """Classify the incoming webhook event into failure_reason + raw_event_type (R13).

    Input:  a raw mandate_failures case dict (as delivered by the webhook/seed).
    Output: the same case dict with `failure_reason` and `raw_event_type` normalized,
            plus a `diagnosis` summary dict. Logs a `webhook_received` audit event.
    """

    def __init__(self, ctx):
        self.ctx = ctx

    def verify(self, case):
        """Return True if the case's webhook signature is valid (constant-time check).

        An event with a missing/invalid signature is treated as spoofed/tampered and
        must be rejected before any other processing.
        """
        return webhook_security.verify_signature(case, case.get("webhook_signature"))

    def reject(self, case):
        """Log a spoofed/invalid-signature event as rejected; never enters the pipeline."""
        self.ctx.set_status(case, "rejected")
        self.ctx.log(
            case, "webhook_rejected",
            f"Rejected webhook: {case.get('raw_event_type') or 'unknown'}",
            "rejected", 0,
            "REJECTED: invalid signature. HMAC-SHA256 verification failed for this "
            "webhook payload, so it was blocked at ingestion and never entered the "
            "recovery pipeline.",
            "rejected")

    def process(self, case):
        raw_event = case.get("raw_event_type")
        reason = case.get("failure_reason")

        # Fill in whichever side is missing so the pair is always consistent.
        if not reason and raw_event:
            reason = WEBHOOK_TO_REASON.get(raw_event, "insufficient_funds")
            case["failure_reason"] = reason
        if not raw_event:
            raw_event = REASON_TO_WEBHOOK.get(reason, "payment.failed")
            case["raw_event_type"] = raw_event

        self.ctx.log(
            case, "webhook_received", f"Triggered by: {raw_event} webhook", "n/a", 0,
            f"Failure ingested from Razorpay webhook '{raw_event}' "
            f"(mapped to failure_reason '{reason}').",
            case.get("case_status", "new"))

        return {"failure_reason": reason, "raw_event_type": raw_event}


# ---------------------------------------------------------------------------
# Stage 2: TriageAgent
# ---------------------------------------------------------------------------
class TriageAgent:
    """Compute recoverability + health scores and decide processing order (R6/R17).

    `process(case)` scores a single case. `order(cases)` returns the triage order
    (highest recoverability score first) used by the pipeline runner.
    """

    def __init__(self, ctx):
        self.ctx = ctx

    def process(self, case):
        score, factors = scoring.score_case(case)
        h_score = health_module.health_score(
            case.get("past_payment_success_rate", 0.0),
            case.get("past_retry_count", 0))
        h_band = health_module.health_band(h_score)
        return {
            "score": score,
            "factors": factors,
            "ground_truth": scoring.explain_score(case, score, factors),
            "health_score": h_score,
            "health_band": h_band,
        }

    @staticmethod
    def order(cases):
        """Triage: process highest recoverability-score cases first (R6)."""
        return sorted(cases, key=lambda c: scoring.score_case(c)[0], reverse=True)


# ---------------------------------------------------------------------------
# Stage 4 collaborator: CommunicationAgent
# ---------------------------------------------------------------------------
class CommunicationAgent:
    """Generate LLM-narrated reasoning + nudge messages, and manage dunning (R9/R16).

    Narration only: it turns already-decided, rule-based facts into readable text and
    advances the Day 1 / Day 3 / Day 7 dunning sequence. It never makes a decision.
    StrategyAgent calls into this collaborator at the appropriate points.
    """

    def __init__(self, ctx):
        self.ctx = ctx

    def reasoning_for_score(self, case, triage):
        """LLM-narrated explanation of the score event, grounded in rule facts."""
        return llm_client.generate_reasoning(case, {
            "event_type": "score",
            "score": triage["score"],
            "score_factors": triage["factors"],
            "strategy": _STRATEGY_LABELS.get(case.get("failure_reason", ""),
                                             "reason-specific recovery"),
            "ground_truth": triage["ground_truth"],
        })

    def messages_for(self, case):
        """LLM-authored Standard + Hinglish nudge variants (template fallback)."""
        return llm_client.generate_message_variants(case)

    def send_dunning(self, case, attempt):
        """R16: advance and log the next dunning stage (Day 1 / Day 3 / Day 7)."""
        current = int(case.get("dunning_stage", 0) or 0)
        if current >= len(DUNNING_STAGES):
            return
        stage = DUNNING_STAGES[current]
        case["dunning_stage"] = stage["stage"]
        db.update_case(self.ctx.conn, case["customer_id"], dunning_stage=stage["stage"])
        msgs = self.messages_for(case)
        reasoning = (
            f"Dunning stage {stage['stage']} of 3 (Day {stage['day']}, {stage['tone']}). "
            f"Message (standard): {msgs['standard']} | Hinglish: {msgs['hinglish']}"
        )
        self.ctx.log(case, "dunning_stage",
                     f"Dunning stage {stage['stage']}/3 sent ({stage['tone']})",
                     "pending", attempt, reasoning,
                     case.get("case_status", "in_progress"))

    def send_reauth_link(self, case):
        """Re-auth link + nudge used by the mandate_expired / mandate-limit paths."""
        msgs = self.messages_for(case)
        self.ctx.log(case, "reauth_link", "Sent re-authorization link + nudge",
                     "pending", 0,
                     f"Re-auth link issued. Nudge (standard): {msgs['standard']}",
                     "in_progress")


# ---------------------------------------------------------------------------
# Stage 3: StrategyAgent
# ---------------------------------------------------------------------------
class StrategyAgent:
    """Apply strategy, mandate-limit gate, retry cap, promises, RBI compliance.

    This stage owns every decision. It writes an audit row for every transition and
    delegates only the narration/messaging/dunning to a CommunicationAgent.
    """

    def __init__(self, ctx, comms):
        self.ctx = ctx
        self.comms = comms

    # --- primitives (decision-bearing) --------------------------------------
    def attempt_retry(self, case, score, attempt, event_type, action_label, window=None):
        """Simulate one debit attempt. Returns True on success. Logs the attempt."""
        prob = _success_prob(case, score)
        success = self.ctx.rng.random() < prob
        win_txt = ""
        if window is not None:
            win_txt = f" during {window['label']} window (days {window['window'][0]}-{window['window'][1]})"
        if success:
            reasoning = (
                f"Attempt {attempt} of {MAX_RETRIES}: retried{win_txt}; "
                f"succeeded (est. {int(prob * 100)}% based on score {score})."
            )
            self.ctx.log(case, event_type, action_label, "success", attempt, reasoning, "recovered")
            self.ctx.set_status(case, "recovered")
            return True
        reasoning = (
            f"Attempt {attempt} of {MAX_RETRIES}: retried{win_txt}; "
            f"failed (est. {int(prob * 100)}% based on score {score})."
        )
        self.ctx.log(case, event_type, action_label, "failure", attempt, reasoning, "in_progress")
        return False

    def escalate(self, case, attempt, reason):
        self.ctx.log(case, "escalate", "Escalated to manual recovery", "n/a", attempt, reason, "escalated")
        self.ctx.set_status(case, "escalated")

    def pre_debit_notification(self, case, attempt):
        """R15: log a pre-debit notice >=24h before a retry and set compliance status.

        The scheduler always aims for a compliant 24h gap; a small minority of cases
        are deliberately non-compliant to make the badge meaningful (short-notice
        retries occur when a mandate is near expiry on high-value insurance/EMI cases).
        """
        non_compliant = (case.get("past_payment_success_rate", 1.0) < 0.4
                         and float(case.get("amount", 0)) > 3000
                         and self.ctx.rng.random() < 0.5)
        notice_hours = 12 if non_compliant else RBI_MIN_NOTICE_HOURS
        status = "non-compliant" if non_compliant else "RBI-compliant"
        case["compliance_status"] = status
        db.update_case(self.ctx.conn, case["customer_id"], compliance_status=status)
        reasoning = (
            f"Pre-debit notification issued {notice_hours}h before retry attempt {attempt}. "
            f"RBI requires >= {RBI_MIN_NOTICE_HOURS}h notice, so this case is {status}."
        )
        self.ctx.log(case, "pre_debit_notification",
                     f"Sent pre-debit notification ({notice_hours}h notice)",
                     "success" if not non_compliant else "failure",
                     attempt, reasoning, case.get("case_status", "in_progress"))

    def maybe_promise(self, case, attempt):
        """Optionally record a promise-to-pay and resolve it. Returns 'kept',
        'broken', or None if no promise was made."""
        if self.ctx.rng.random() >= PROMISE_OFFER_PROB:
            return None
        promised_by = (datetime.now() + timedelta(days=3)).date().isoformat()
        self.ctx.set_status(case, "promised")
        self.ctx.log(case, "promise_recorded", f"Customer promised to pay by {promised_by}",
                     "pending", attempt,
                     f"Customer committed to pay Rs {int(case['amount'])} by {promised_by}.",
                     "promised")
        if self.ctx.rng.random() < PROMISE_KEPT_PROB:
            self.ctx.log(case, "promise_kept", "Promise kept; payment received", "success",
                         attempt, "Customer paid within the promised window.", "recovered")
            self.ctx.set_status(case, "recovered")
            return "kept"
        self.ctx.log(case, "promise_broken", "Promise broken; payment not received", "failure",
                     attempt, f"No payment by {promised_by}; routing to broken-promise path.",
                     "broken_promise")
        self.ctx.set_status(case, "broken_promise")
        return "broken"

    # --- orchestration for one case -----------------------------------------
    def process(self, case, triage):
        """Run the full strategy/retry flow for one case, logging every transition.

        `triage` is the TriageAgent output for this case. The score-event reasoning is
        narrated by the CommunicationAgent over the rule-based ground truth; the score
        and every downstream decision remain deterministic.
        """
        score = triage["score"]

        # Score event (R6/R7). Deterministic score, LLM-narrated reasoning text.
        reasoning_text = self.comms.reasoning_for_score(case, triage)
        self.ctx.set_status(case, "in_progress")
        self.ctx.log(case, "score", f"Computed recoverability score {score}", "n/a", 0,
                     reasoning_text, "in_progress")

        reason = case.get("failure_reason", "")

        # mandate_revoked -> immediate escalation, no retry (R1).
        if reason == "mandate_revoked":
            self.ctx.log(case, "strategy_selected", "Strategy: immediate escalation", "n/a", 0,
                         "Mandate was revoked by the customer; retrying is not permitted, so the "
                         "case is escalated immediately for manual re-consent.", "in_progress")
            self.escalate(case, 0, "Mandate revoked; no retry possible, escalated for manual follow-up.")
            return score

        # UPI mandate-limit gate (R14): an over-limit amount cannot be auto-debited
        # under the existing mandate, so skip normal retry and route to higher-limit
        # re-authorization (handled like mandate_expired), regardless of failure_reason.
        mandate_limit = float(case.get("mandate_limit", 5000) or 5000)
        if float(case.get("amount", 0)) > mandate_limit:
            self.ctx.log(case, "mandate_limit_block",
                         "Blocked: amount exceeds UPI mandate limit", "n/a", 0,
                         f"Amount Rs {int(case['amount'])} exceeds the UPI mandate limit of "
                         f"Rs {int(mandate_limit)}; a normal retry is not permitted. Requires "
                         f"mandate re-authorization at a higher limit before any debit.",
                         "in_progress")
            self.ctx.log(case, "strategy_selected",
                         "Strategy: higher-limit re-authorization", "n/a", 0,
                         "Routing like an expired mandate: send a re-auth link to raise the "
                         "mandate limit before retrying.", "in_progress")
            self._nudge_and_retry(case, score, "Retried debit after higher-limit re-auth")
            return score

        # Reason-specific strategy selection + retry loop with hard cap (R1/R2).
        if reason == "insufficient_funds":
            window = salary_window.infer_window(case)
            self.ctx.log(case, "strategy_selected", "Strategy: salary-window retry", "n/a", 0,
                         f"Insufficient funds; scheduling retries in the {window['reason']}.",
                         "in_progress")
            self._retry_loop(case, score, event_type="retry",
                             action_label="Retried debit in salary window",
                             window=window, dunning=True)
        elif reason == "mandate_expired":
            self.ctx.log(case, "strategy_selected", "Strategy: re-authorization link", "n/a", 0,
                         "Mandate expired; sending a re-auth link before any retry.", "in_progress")
            self._nudge_and_retry(case, score, "Retried debit after re-auth link")
        elif reason == "bank_technical_error":
            self.ctx.log(case, "strategy_selected", "Strategy: silent quick retry", "n/a", 0,
                         "Transient bank error; performing silent quick retries with no customer nudge.",
                         "in_progress")
            self._retry_loop(case, score, event_type="silent_retry",
                             action_label="Silent quick retry")
        return score

    def _retry_loop(self, case, score, event_type, action_label, window=None, dunning=False):
        """Attempt up to MAX_RETRIES retries; escalate on exhaustion (R2).

        Before each attempt an RBI pre-debit notification is logged (R15). When
        `dunning` is set, a staged nudge (R16) is sent before each attempt too. A
        promise-to-pay may be offered between attempts (R3); a kept promise ends the
        loop as recovered, a broken promise consumes the remaining budget then escalates.
        """
        for attempt in range(1, MAX_RETRIES + 1):
            if dunning:
                self.comms.send_dunning(case, attempt)
            self.pre_debit_notification(case, attempt)
            if self.attempt_retry(case, score, attempt, event_type, action_label, window):
                return  # recovered

            # Between attempts, the customer may make a promise-to-pay.
            if attempt < MAX_RETRIES:
                outcome = self.maybe_promise(case, attempt)
                if outcome == "kept":
                    return
                if outcome == "broken":
                    # Broken promise: one more retry if budget remains, else escalate.
                    last_attempt = attempt
                    if attempt + 1 <= MAX_RETRIES:
                        last_attempt = attempt + 1
                        self.pre_debit_notification(case, last_attempt)
                        if self.attempt_retry(case, score, last_attempt, event_type,
                                              action_label + " (post-broken-promise)", window):
                            return
                    self.escalate(case, last_attempt,
                                  "Promise broken and retry budget exhausted; escalated.")
                    return

        # Hard cap reached without recovery -> mandatory escalation.
        self.escalate(case, MAX_RETRIES,
                      f"Reached the {MAX_RETRIES}-retry cap without recovery; "
                      f"mandatory escalation per policy.")

    def _nudge_and_retry(self, case, score, action_label):
        """Re-auth path shared by mandate_expired and the mandate-limit gate (R14/R16).

        Sends a re-auth link (via CommunicationAgent), then runs the dunning-enabled
        retry loop.
        """
        self.comms.send_reauth_link(case)
        self._retry_loop(case, score, event_type="retry",
                         action_label=action_label, dunning=True)


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------
class RecoveryPipeline:
    """Wires the four agents together for a run: Diagnosis -> Triage -> Strategy."""

    def __init__(self, conn, rng):
        self.ctx = _RunContext(conn, rng)
        self.diagnosis = DiagnosisAgent(self.ctx)
        self.triage = TriageAgent(self.ctx)
        self.comms = CommunicationAgent(self.ctx)
        self.strategy = StrategyAgent(self.ctx, self.comms)

    def process_case(self, case):
        """Run one case through the full pipeline. Returns its score, or None if rejected.

        Signature verification is the very first gate: a spoofed/invalid-signature
        event is logged as rejected and never reaches Triage/Strategy (so it draws no
        RNG and cannot affect any recovery outcome).
        """
        if not self.diagnosis.verify(case):
            self.diagnosis.reject(case)
            return None
        self.diagnosis.process(case)
        triage = self.triage.process(case)
        self.strategy.process(case, triage)
        return triage["score"]

    def process_case_traced(self, case):
        """Same as process_case, but returns a per-stage trace for the live view.

        The trace is a read-only summary of what each agent produced for this case;
        it does not change any decision, RNG draw, or audit row. Returned dict:
        {customer_id, diagnosis, score, health_band, strategy, final_status}.
        Rejected (spoofed) events return a trace with final_status='rejected'.
        """
        if not self.diagnosis.verify(case):
            self.diagnosis.reject(case)
            return {
                "customer_id": case["customer_id"],
                "diagnosis": case.get("failure_reason"),
                "raw_event_type": case.get("raw_event_type"),
                "score": None,
                "health_band": None,
                "strategy": "rejected: invalid signature",
                "final_status": "rejected",
                "amount": float(case.get("amount", 0)),
            }
        diag = self.diagnosis.process(case)
        triage = self.triage.process(case)
        strategy_label = _STRATEGY_LABELS.get(case.get("failure_reason", ""),
                                              "reason-specific recovery")
        self.strategy.process(case, triage)
        return {
            "customer_id": case["customer_id"],
            "diagnosis": diag["failure_reason"],
            "raw_event_type": diag["raw_event_type"],
            "score": triage["score"],
            "health_band": triage["health_band"],
            "strategy": strategy_label,
            "final_status": case.get("case_status"),
            "amount": float(case.get("amount", 0)),
        }


def _apply_llm_budget(cases):
    """Mark the top-N highest-value cases for live LLM narration (rest = templates).

    Does not affect any decision or RNG draw; it only decides which cases get
    LLM-authored text versus deterministic template text.
    """
    if LLM_LIVE_TOP_N and LLM_LIVE_TOP_N > 0:
        top = sorted(cases, key=lambda c: float(c.get("amount", 0)), reverse=True)
        llm_client.set_live_budget(c["customer_id"] for c in top[:LLM_LIVE_TOP_N])
    else:
        llm_client.set_live_budget(None)  # no cap: every case may use the LLM


def run_agent():
    """Score every case, process in descending-score order (triage), return a summary."""
    rng = random.Random(RUN_SEED)
    conn = db.get_connection()
    try:
        cases = db.get_all_cases(conn)
        _apply_llm_budget(cases)
        # Triage: compute scores first, then process highest-value cases first (R6).
        scored = TriageAgent.order(cases)
        pipeline = RecoveryPipeline(conn, rng)
        processed = 0
        for case in scored:
            pipeline.process_case(case)
            processed += 1
        conn.commit()

        statuses = {}
        for row in db.get_all_cases(conn):
            statuses[row["case_status"]] = statuses.get(row["case_status"], 0) + 1
    finally:
        conn.close()
    return {"processed": processed, "status_counts": statuses}


def run_agent_traced():
    """Generator version of run_agent for the live pipeline view.

    Yields one per-case trace dict as each case finishes processing, then a final
    summary dict {done: True, processed, status_counts}. Uses the identical seeded
    RNG and triage order as run_agent(), so outcomes are unchanged. The caller is
    responsible for pacing (visual delays) — this generator itself does not sleep.
    """
    rng = random.Random(RUN_SEED)
    conn = db.get_connection()
    try:
        all_cases = db.get_all_cases(conn)
        _apply_llm_budget(all_cases)
        cases = TriageAgent.order(all_cases)
        pipeline = RecoveryPipeline(conn, rng)
        processed = 0
        for case in cases:
            trace = pipeline.process_case_traced(case)
            processed += 1
            yield trace
        conn.commit()
        statuses = {}
        for row in db.get_all_cases(conn):
            statuses[row["case_status"]] = statuses.get(row["case_status"], 0) + 1
    finally:
        conn.close()
    yield {"done": True, "processed": processed, "status_counts": statuses}


if __name__ == "__main__":
    summary = run_agent()
    print("Agent run complete:", summary)
