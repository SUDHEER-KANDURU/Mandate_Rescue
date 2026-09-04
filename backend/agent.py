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
# Phase 4: execution service imports.  Imported lazily inside _attempt_real_execution
# to keep the existing simulation path unchanged and avoid a hard startup dependency
# on payment_executor when running benchmarks / the Policy Sandbox.
_payment_executor_mod = None
_scheduler_mod = None


def _import_executor():
    """Lazy import so benchmark/simulation paths never load payment_executor."""
    global _payment_executor_mod, _scheduler_mod
    if _payment_executor_mod is None:
        import payment_executor as _pe
        import scheduler as _sc
        _payment_executor_mod = _pe
        _scheduler_mod = _sc
    return _payment_executor_mod, _scheduler_mod


MAX_RETRIES = 3
RUN_SEED = 42

# Default recoverability weights (must match scoring.py). Exposed so the Policy
# Sandbox / tests can construct a PolicyParams without importing scoring.
DEFAULT_SCORE_WEIGHTS = {
    "success": scoring.W_SUCCESS,
    "tenure": scoring.W_TENURE,
    "retry": scoring.W_RETRY,
    "reason": scoring.W_REASON,
}


class PolicyParams:
    """Optional policy overlay for the sandbox and tests.

    The live dashboard run uses defaults (retry cap 3, module score weights,
    adaptive salary windows, LLM narration allowed). Passing PolicyParams into
    run_agent / RecoveryPipeline must not change RNG draw order when those
    fields equal the defaults — that is what keeps 139/38/3 stable.
    """

    def __init__(self, retry_cap=None, score_weights=None,
                 salary_window_mode="adaptive", use_llm=True,
                 execution_mode=None):
        self.retry_cap = MAX_RETRIES if retry_cap is None else int(retry_cap)
        self.score_weights = (
            dict(DEFAULT_SCORE_WEIGHTS) if score_weights is None
            else dict(score_weights)
        )
        self.salary_window_mode = salary_window_mode or "adaptive"
        self.use_llm = bool(use_llm)
        # execution_mode: None = use scheduler.execution_mode_for_case() per-case
        # (default behaviour); "simulation" = force simulation for all cases in
        # this run (benchmarks, Policy Sandbox); "real_test" = force real execution.
        self.execution_mode = execution_mode  # None | "simulation" | "real_test"

    def normalized_weights(self):
        """Return score_weights as a dict (for serialization to the API)."""
        return dict(self.score_weights)

    def is_default(self):
        """True when this policy is equivalent to the module defaults."""
        return (
            self.retry_cap == MAX_RETRIES
            and self.score_weights == DEFAULT_SCORE_WEIGHTS
            and self.salary_window_mode == "adaptive"
        )


# Module-level default policy singleton. Simulation/sandbox code uses this as the
# reference "current production policy" to compare against.
DEFAULT_POLICY = PolicyParams()


def replace(policy, **kwargs):
    """Return a new PolicyParams with selected fields overridden.

    Mirrors dataclasses.replace() but for the plain PolicyParams class. Only the
    keyword arguments supplied are changed; the rest copy from `policy`.
    Primarily used by simulation_runner to force use_llm=False without changing
    any other policy parameter.
    """
    return PolicyParams(
        retry_cap=kwargs.get("retry_cap", policy.retry_cap),
        score_weights=kwargs.get("score_weights", policy.score_weights),
        salary_window_mode=kwargs.get("salary_window_mode", policy.salary_window_mode),
        use_llm=kwargs.get("use_llm", policy.use_llm),
        execution_mode=kwargs.get("execution_mode", policy.execution_mode),
    )

# Cost/latency control for live runs: generate LLM narration/messages live for only
# the top-N highest-value cases; the rest use deterministic templates. This keeps a
# full 180-case run inside the demo's latency budget against a real remote LLM.
# Lowered to 5 (from 20) to stay within the 8K TPM free-tier Groq limit.
# Set LLM_LIVE_TOP_N=20 in .env for broader narration when rate limits allow.
import os as _os
LLM_LIVE_TOP_N = int(_os.environ.get("LLM_LIVE_TOP_N", "5"))

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


# Ingestion input-validation bounds. An auto-debit amount must be a finite, strictly
# positive rupee value within a sane ceiling. A negative / zero / NaN / absurd amount
# is a malformed or malicious event: if it were processed normally it could silently
# corrupt every money total (a negative "amount recovered" would understate the
# dashboard). Such events are rejected at ingestion to an 'invalid' status and never
# enter the scoring/retry pipeline or the money aggregates. This mirrors the existing
# signature gate and is additive: valid clean-data cases are unaffected.
MAX_REASONABLE_AMOUNT = 10_000_000.0  # Rs 1 crore ceiling for a single mandate debit.


def validate_case(case):
    """Return (ok: bool, reason: str|None) for a case's ingestion validity.

    Checks the amount is a finite, strictly positive, non-absurd number. Returns
    (True, None) for a valid case, or (False, human-readable reason) otherwise. Never
    raises — a non-numeric amount is treated as invalid, not an exception.
    """
    raw = case.get("amount", None)
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        return False, f"non-numeric amount ({raw!r})"
    if amount != amount or amount in (float("inf"), float("-inf")):  # NaN / inf
        return False, f"non-finite amount ({raw!r})"
    if amount <= 0:
        return False, f"non-positive amount (Rs {amount:.2f}); must be > 0"
    if amount > MAX_REASONABLE_AMOUNT:
        return False, (f"implausibly large amount (Rs {amount:.2f}); "
                       f"exceeds the Rs {MAX_REASONABLE_AMOUNT:.0f} ceiling")
    return True, None


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

    def __init__(self, conn, rng, policy=None):
        self.conn = conn
        self.rng = rng
        self.policy = policy   # Phase 4: execution mode comes from here
        self._tick = 0
        self._exec_mode_cache = {}  # customer_id → 'real_test'|'simulation'

    def ts(self):
        """Monotonic timestamp so audit rows keep insertion order within a case."""
        self._tick += 1
        return _now_iso(self._tick)

    def log(self, case, event_type, action, outcome, attempt, reasoning, status_after):
        db.insert_audit(self.conn, case["customer_id"], self.ts(), event_type,
                        action, outcome, attempt, reasoning, status_after)

    def set_status(self, case, status):
        """Transition case to `status`, enforcing legal state machine transitions.

        Logs the transition to state_transitions for a durable, query-friendly
        history separate from the audit_log narrative. Rejects illegal transitions
        (e.g. recovered -> in_progress) with a ValueError so a bug in the pipeline
        logic is surfaced immediately rather than silently corrupting state.
        """
        current = case.get("case_status", "new")
        if current == status:
            # No-op: already in the target state. This is legal (idempotent).
            return
        if not db.is_legal_transition(current, status):
            raise ValueError(
                f"Illegal state transition {current!r} -> {status!r} for "
                f"customer {case.get('customer_id')!r}. "
                f"Legal from {current!r}: {db.LEGAL_TRANSITIONS.get(current, set())}"
            )
        case["case_status"] = status
        db.update_case(self.conn, case["customer_id"], case_status=status)
        db.record_state_transition(
            self.conn, case["customer_id"], current, status,
            triggered_by="agent_pipeline",
        )


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

    def validate_input(self, case):
        """Return (ok, reason) for the case's data validity (see validate_case)."""
        return validate_case(case)

    def reject_invalid(self, case, reason):
        """Log a malformed event (e.g. bad amount) as invalid; never enters the pipeline.

        Kept distinct from signature rejection so the two failure modes are
        separately auditable. An 'invalid' case is excluded from every money total by
        metrics.py, so a corrupt amount can never move a dashboard figure."""
        self.ctx.set_status(case, "invalid")
        self.ctx.log(
            case, "webhook_invalid",
            f"Rejected malformed webhook: {reason}",
            "rejected", 0,
            f"REJECTED: invalid input data ({reason}). The event failed ingestion "
            f"validation and never entered the recovery pipeline, so it cannot affect "
            f"any recovery outcome or money total.",
            "invalid")

    def note_duplicate(self, case):
        """Log a redelivered event for a case that is already terminal; do not rescore."""
        status = case.get("case_status", "new")
        self.ctx.log(
            case, "webhook_duplicate",
            "Duplicate webhook ignored; case already in a terminal state",
            "n/a", 0,
            "Idempotency: this case already has a terminal audit event "
            f"({status}); skipped scoring and recovery so a replay cannot "
            "change the outcome or consume another RNG draw.",
            status)

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
        """Execute one debit attempt via the explicit execution service.

        SIMULATION mode  (synthetic cases / benchmarks / Policy Sandbox):
            Uses the seeded RNG draw — behaviour identical to the original
            implementation. Clearly labeled in audit_log.reasoning_text.

        REAL_TEST mode  (razorpay_live cases with configured credentials):
            Delegates to PaymentExecutionService which calls the Razorpay
            Test API.  Outcome comes from the real API response — never
            from an RNG draw.  If execution fails the job is logged as
            failed, not silently converted to success.

        The mode is resolved once per case via the policy or
        scheduler.execution_mode_for_case() and stored on the _RunContext
        so all attempts for the same case use the same mode.
        """
        exec_mode = self._resolve_exec_mode(case)
        prob = _success_prob(case, score)
        win_txt = ""
        if window is not None:
            win_txt = (f" during {window['label']} window "
                       f"(days {window['window'][0]}-{window['window'][1]})")

        if exec_mode == "real_test":
            success, result_text, ext_ids = self._attempt_real_execution(
                case, attempt, prob)
        else:
            # Simulation path — unchanged RNG draw, always labeled as simulation.
            success = self.ctx.rng.random() < prob
            result_text = (
                f"[SIMULATION — synthetic run, not a real payment] "
                f"Attempt {attempt} of {MAX_RETRIES}: retried{win_txt}; "
                f"{'succeeded' if success else 'failed'} "
                f"(simulated prob={int(prob * 100)}%, score={score})."
            )
            ext_ids = {}

        # Build audit reasoning, appending real execution IDs where present.
        id_suffix = ""
        if ext_ids.get("razorpay_payment_id"):
            id_suffix += f" Payment ID: {ext_ids['razorpay_payment_id']}."
        if ext_ids.get("payment_link_url"):
            id_suffix += f" Link: {ext_ids['payment_link_url']}."

        if success:
            reasoning = result_text + id_suffix
            self.ctx.log(case, event_type, action_label, "success",
                         attempt, reasoning, "recovered")
            self.ctx.set_status(case, "recovered")
            return True

        reasoning = result_text + id_suffix
        self.ctx.log(case, event_type, action_label, "failure",
                     attempt, reasoning, "in_progress")
        return False

    def _resolve_exec_mode(self, case: dict) -> str:
        """Return 'real_test' or 'simulation' for this case.

        Priority: policy.execution_mode → scheduler.execution_mode_for_case().
        Cached on the _RunContext per case so all attempts are consistent.
        """
        # Check for a context-level cached mode (set by the pipeline orchestrator).
        cached = getattr(self.ctx, "_exec_mode_cache", {})
        cust = case.get("customer_id", "")
        if cust in cached:
            return cached[cust]

        policy = getattr(self.ctx, "policy", None)
        if policy is not None and policy.execution_mode is not None:
            mode = policy.execution_mode
        else:
            try:
                _, sched = _import_executor()
                from payment_executor import ExecutionMode
                em = sched.execution_mode_for_case(case)
                mode = em.value
            except Exception:
                mode = "simulation"

        if not hasattr(self.ctx, "_exec_mode_cache"):
            self.ctx._exec_mode_cache = {}
        self.ctx._exec_mode_cache[cust] = mode
        return mode

    def _attempt_real_execution(self, case: dict, attempt: int,
                                 success_prob: float) -> tuple:
        """Call PaymentExecutionService for a real Razorpay Test Mode attempt.

        Returns (success: bool, result_text: str, ext_ids: dict).
        Never raises — any exception is caught and treated as a failed attempt.
        """
        try:
            pe, _ = _import_executor()
            from payment_executor import ExecutionMode
            executor = pe.get_executor(rng=self.ctx.rng)
            result = executor.execute_recovery(
                case=case,
                attempt=attempt,
                execution_mode=ExecutionMode.REAL_TEST,
                success_prob=success_prob,
            )
            success = result.success
            result_text = (
                f"[REAL TEST MODE — Razorpay Test API] "
                f"Attempt {attempt} of {MAX_RETRIES}: "
                f"{result.outcome.value}."
            )
            if result.failure_reason:
                result_text += f" Reason: {result.failure_reason}."
            ext_ids = {
                "razorpay_payment_id": result.razorpay_payment_id,
                "payment_link_url": result.payment_link_url,
                "razorpay_payment_link_id": result.razorpay_payment_link_id,
            }
            return success, result_text, ext_ids
        except Exception as exc:
            import logging as _log
            _log.getLogger("mandate_rescue.agent").error(
                "Real execution failed for %s attempt %d: %s",
                case.get("customer_id"), attempt, exc, exc_info=True,
            )
            return False, (
                f"[REAL TEST MODE — execution error] "
                f"Attempt {attempt}: {exc}. Treated as failed attempt."
            ), {}

    def escalate(self, case, attempt, reason):
        self.ctx.log(case, "escalate", "Escalated to manual recovery", "n/a", attempt, reason, "escalated")
        self.ctx.set_status(case, "escalated")

    def pre_debit_notification(self, case, attempt):
        """R15: log a pre-debit notice >=24h before a retry and set compliance status.

        The scheduler always aims for a compliant 24h gap; a small minority of cases
        are deliberately non-compliant to make the badge meaningful (short-notice
        retries occur when a mandate is near expiry on high-value insurance/EMI cases).

        When `notification_ts` and `scheduled_retry_ts` are present on the case dict
        (e.g. injected by tests or real integrations), the actual gap is computed and
        used for the compliance check instead of the stochastic heuristic. This keeps
        the chaos-test clock-skew scenario exercising real compliance logic.
        """
        # If the case carries explicit notification/retry timestamps, derive compliance
        # from the real gap rather than the stochastic heuristic.
        notice_ts = case.get("notification_ts")
        retry_ts  = case.get("scheduled_retry_ts")
        if notice_ts and retry_ts:
            try:
                from datetime import datetime as _dt
                fmt = "%Y-%m-%dT%H:%M:%S"
                t_notice = _dt.strptime(str(notice_ts)[:19], fmt)
                t_retry  = _dt.strptime(str(retry_ts)[:19], fmt)
                gap_hours = (t_retry - t_notice).total_seconds() / 3600.0
                non_compliant = gap_hours < RBI_MIN_NOTICE_HOURS
                notice_hours  = max(0, int(gap_hours))
            except (ValueError, TypeError):
                non_compliant = False
                notice_hours  = RBI_MIN_NOTICE_HOURS
        else:
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
        # Safe amount conversion: amount may be a string, float, or None depending on
        # the data source (seed vs. webhook).  Never call int() on raw dict value.
        _amount = int(round(float(case.get("amount") or 0)))
        self.ctx.set_status(case, "promised")
        self.ctx.log(case, "promise_recorded", f"Customer promised to pay by {promised_by}",
                     "pending", attempt,
                     f"Customer committed to pay Rs {_amount} by {promised_by}.",
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
        if float(case.get("amount", 0) or 0) > mandate_limit:
            _amt = int(round(float(case.get("amount", 0) or 0)))
            self.ctx.log(case, "mandate_limit_block",
                         "Blocked: amount exceeds UPI mandate limit", "n/a", 0,
                         f"Amount Rs {_amt} exceeds the UPI mandate limit of "
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


# Terminal audit statuses: a case with any of these in audit_log.case_status_after
# has already finished the pipeline. A replay must not score or retry it again.
_TERMINAL_AUDIT_STATUSES = frozenset({"recovered", "escalated", "rejected"})


def _schedule_jobs_for_case(conn, case: dict, policy) -> None:
    """Phase 4: schedule durable recovery jobs after strategy.process() completes.

    Only schedules for cases that still need future execution — i.e., cases where
    the synchronous simulation ran a retry loop (mandate_revoked and immediately-
    recovered cases don't need scheduled jobs).

    For simulation runs (synthetic data / Policy Sandbox / benchmarks):
      - Jobs are created with execution_mode='simulation' so the scheduler
        worker also runs the simulation path, not a real API call.

    For razorpay_live cases with configured credentials:
      - Jobs are created with execution_mode='real_test'.

    This is idempotent: create_recovery_job uses a UNIQUE idempotency_key, so
    calling this twice for the same case is always safe.
    """
    # Don't schedule for terminal cases — the synchronous path already resolved them.
    final_status = case.get("case_status", "new")
    if final_status in ("recovered", "escalated", "rejected", "invalid"):
        return
    # Don't schedule for mandate_revoked — policy says no retry.
    if case.get("failure_reason") == "mandate_revoked":
        return

    try:
        _, sched = _import_executor()
        from payment_executor import ExecutionMode

        # Determine execution mode: policy override → per-case auto-detect.
        if policy is not None and policy.execution_mode is not None:
            try:
                exec_mode = ExecutionMode(policy.execution_mode)
            except ValueError:
                exec_mode = ExecutionMode.SIMULATION
        else:
            exec_mode = sched.execution_mode_for_case(case)

        max_ret = policy.retry_cap if policy is not None else MAX_RETRIES
        sched.schedule_recovery_jobs(
            conn=conn,
            case=case,
            execution_mode=exec_mode,
            max_retries=max_ret,
        )
    except Exception as exc:
        # Scheduling failure must never abort the pipeline transaction.
        import logging as _log
        _log.getLogger("mandate_rescue.agent").warning(
            "Could not schedule recovery jobs for %s: %s",
            case.get("customer_id"), exc,
        )


def _has_terminal_audit(conn, customer_id):
    trail = db.get_audit_for_case(conn, customer_id)
    return any(row.get("case_status_after") in _TERMINAL_AUDIT_STATUSES for row in trail)


def _acquire_processing_lock(conn, customer_id):
    """Attempt to acquire an exclusive processing lock for this case.

    Uses BEGIN IMMEDIATE so that two concurrent workers cannot both pass the
    idempotency check at the same time. Returns True if the lock was acquired,
    False if another worker already holds it (i.e., the case is being or has been
    processed).

    The lock is automatically released when the caller's transaction commits or
    rolls back — there is no separate unlock step.
    """
    import logging as _log
    try:
        # BEGIN IMMEDIATE causes SQLite to upgrade to a reserved lock right away,
        # serializing any concurrent writers at the point of the check. Two separate
        # connections racing here will serialize at the DB level.
        conn.execute("BEGIN IMMEDIATE")
    except Exception as _lock_exc:
        # OperationalError "cannot start a transaction within a transaction" is
        # expected when the connection is already inside a transaction (e.g. tests
        # using an in-memory DB). Log at DEBUG — this is not an error, but we do
        # NOT swallow it silently with a bare pass so unusual exceptions are visible.
        _log.getLogger("mandate_rescue.agent").debug(
            "BEGIN IMMEDIATE skipped for %s: %s",
            customer_id, _lock_exc,
        )
    return not _has_terminal_audit(conn, customer_id)


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------
class RecoveryPipeline:
    """Wires the four agents together for a run: Diagnosis -> Triage -> Strategy."""

    def __init__(self, conn, rng, policy=None):
        self.ctx = _RunContext(conn, rng, policy=policy)
        self.policy = policy
        self.diagnosis = DiagnosisAgent(self.ctx)
        self.triage = TriageAgent(self.ctx)
        self.comms = CommunicationAgent(self.ctx)
        self.strategy = StrategyAgent(self.ctx, self.comms)

    def process_case(self, case):
        """Run one case through the full pipeline. Returns its score, or None if rejected.

        Gates, in order: (1) terminal-audit idempotency — a case that already
        ended recovered/escalated/rejected is logged as webhook_duplicate and
        never rescored (so a second Run agent cannot break the seeded outcome);
        (2) signature verification — a spoofed/invalid-signature event is logged
        rejected; (3) input validation — a malformed event (e.g. negative/zero
        amount) is logged invalid. Only a valid, correctly-signed, not-yet-terminal
        event reaches Triage/Strategy. Each of those gates draws no RNG, so
        recovery outcomes are unaffected.

        Concurrency: _acquire_processing_lock issues BEGIN IMMEDIATE before the
        idempotency check so two concurrent workers cannot both see "not terminal"
        and both proceed. The lock is held until commit/rollback.

        Phase 4: after strategy.process(), schedule recovery jobs for cases whose
        final status warrants future execution (i.e. not immediately resolved).
        """
        if not _acquire_processing_lock(self.ctx.conn, case["customer_id"]):
            self.diagnosis.note_duplicate(case)
            return None
        if not self.diagnosis.verify(case):
            self.diagnosis.reject(case)
            return None
        ok, reason = self.diagnosis.validate_input(case)
        if not ok:
            self.diagnosis.reject_invalid(case, reason)
            return None
        self.diagnosis.process(case)
        triage = self.triage.process(case)
        self.strategy.process(case, triage)
        # Phase 4: schedule durable recovery jobs after strategy decides.
        _schedule_jobs_for_case(self.ctx.conn, case, self.policy)
        return triage["score"]

    def process_case_traced(self, case):
        """Same as process_case, but returns a per-stage trace for the live view.

        The trace is a read-only summary of what each agent produced for this case;
        it does not change any decision, RNG draw, or audit row. Returned dict:
        {customer_id, diagnosis, score, health_band, strategy, final_status}.
        Rejected (spoofed) events return a trace with final_status='rejected'.
        Invalid events return a trace with final_status='invalid'.
        Duplicate deliveries of an already-terminal case return final_status
        unchanged and never call Triage/Strategy (no extra RNG draws).
        """
        if not _acquire_processing_lock(self.ctx.conn, case["customer_id"]):
            self.diagnosis.note_duplicate(case)
            return {
                "customer_id": case["customer_id"],
                "diagnosis": case.get("failure_reason"),
                "raw_event_type": case.get("raw_event_type"),
                "score": None,
                "health_band": None,
                "strategy": "duplicate: already in a terminal state",
                "final_status": case.get("case_status"),
                "amount": float(case.get("amount", 0) or 0),
            }
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
        ok, reason = self.diagnosis.validate_input(case)
        if not ok:
            self.diagnosis.reject_invalid(case, reason)
            return {
                "customer_id": case["customer_id"],
                "diagnosis": case.get("failure_reason"),
                "raw_event_type": case.get("raw_event_type"),
                "score": None,
                "health_band": None,
                "strategy": "rejected: invalid input (" + reason + ")",
                "final_status": "invalid",
                "amount": float(case.get("amount", 0) or 0),
            }
        diag = self.diagnosis.process(case)
        triage = self.triage.process(case)
        strategy_label = _STRATEGY_LABELS.get(case.get("failure_reason", ""),
                                              "reason-specific recovery")
        self.strategy.process(case, triage)
        # Phase 4: schedule durable recovery jobs after strategy decides.
        _schedule_jobs_for_case(self.ctx.conn, case, self.policy)
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


def run_agent(policy=None, conn=None, seed=None):
    """Score every case, process in descending-score order (triage), return a summary.

    `conn` and `seed` are for tests/sandbox: when `conn` is provided it is not
    closed (the caller owns it). `seed` defaults to RUN_SEED so the live
    dashboard outcome stays pinned at 139/38/3. `policy` is accepted for the
    existing sandbox/test call sites; default-equivalent policies do not
    change scoring or RNG order.
    """
    rng = random.Random(RUN_SEED if seed is None else seed)
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        cases = db.get_all_cases(conn)
        if policy is not None and not policy.use_llm:
            llm_client.set_live_budget([], suppress=True)
        else:
            _apply_llm_budget(cases)
        # Triage: compute scores first, then process highest-value cases first (R6).
        scored = TriageAgent.order(cases)
        pipeline = RecoveryPipeline(conn, rng, policy=policy)
        processed = 0
        for case in scored:
            pipeline.process_case(case)
            processed += 1
        conn.commit()

        statuses = {}
        for row in db.get_all_cases(conn):
            statuses[row["case_status"]] = statuses.get(row["case_status"], 0) + 1
    finally:
        if own:
            conn.close()
    return {"processed": processed, "status_counts": statuses}


def run_agent_traced():
    """Generator version of run_agent for the live pipeline view.

    Yields one per-case trace dict as each case finishes processing, then a final
    summary dict {done: True, processed, status_counts}. Uses the identical seeded
    RNG and triage order as run_agent(), so outcomes are unchanged. The caller is
    responsible for pacing (visual delays) — this generator itself does not sleep.

    LLM narration is suppressed during the stream so the generator never blocks on
    a remote Groq call mid-yield. The feed cards don't display reasoning text, so
    nothing visible is lost. The budget is restored to unrestricted after the run
    so drawer/ask endpoints generate narration lazily on demand (with cache).
    """
    rng = random.Random(RUN_SEED)
    conn = db.get_connection()
    # Suppress ALL LLM calls during the live stream. The SSE generator is
    # synchronous — any blocking Groq call (timeout, rate-limit, slow network)
    # starves the stream and causes the UI to freeze partway. Narration is still
    # written to audit_log as the deterministic ground-truth fallback, and the
    # drawer regenerates it (with the LLM) on first open after the run.
    #
    # Use the public save/restore API (never access private module globals directly)
    # so concurrent drawer/ask threads on other Flask worker threads are safe.
    _prior_state = llm_client.save_llm_state()
    llm_client.set_live_budget([], suppress=True)
    try:
        all_cases = db.get_all_cases(conn)
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
        # Restore the budget state that was in effect before this run started.
        llm_client.restore_llm_state(_prior_state)
    yield {"done": True, "processed": processed, "status_counts": statuses}


if __name__ == "__main__":
    summary = run_agent()
    print("Agent run complete:", summary)
