# Mandate Rescue

**AI-powered revenue recovery for failed recurring payments.**

Mandate Rescue diagnoses *why* a UPI Autopay / e-mandate auto-debit failed, decides the
right recovery strategy for that specific failure, executes it under a hard set of
compliance guardrails, and explains every decision it makes — with a full audit trail,
a validated ML layer, and a chaos-tested security posture underneath it.

This README documents **every feature in the project**, module by module, so a reviewer
can understand exactly what each piece does, what's real, and what's simulated.


## 1. The problem

When a recurring UPI Autopay or e-mandate debit fails — insufficient funds, an expired
mandate, a bank glitch, a revoked mandate — most merchants respond the same way
regardless of cause: one generic "payment failed" SMS, then hope. That approach:

- **Wastes retries** on cases that were never going to recover (a revoked mandate will
  never succeed on retry, but gets nagged anyway).
- **Misses recoverable cases** that needed *better timing*, not more attempts (retrying
  an insufficient-funds case on the customer's actual salary date recovers far more
  than retrying blindly).
- **Has no visibility** into *why* a case recovered or didn't.
- **Has no compliance guardrail** — RBI requires a pre-debit notice before every retry,
  and most ad-hoc dunning scripts don't enforce this.

Mandate Rescue replaces "one script for everything" with a diagnose-then-decide
pipeline that is deterministic, auditable, and that a merchant's risk team can actually
trust — every decision is explainable, and every claim about performance is
independently re-verified by the system itself.

---

## 2. What Mandate Rescue does (at a glance)

- **Diagnoses** each failure into a specific reason and computes a recoverability
  score from real, pre-outcome features (historical success rate, tenure, retry
  count, failure type).
- **Decides** a reason-specific strategy: salary-window-timed retry, re-authorization
  link, silent quick retry, or immediate escalation — never a one-size-fits-all
  response.
- **Enforces** hard guardrails: a 3-retry cap, RBI's 24-hour pre-debit notice rule,
  UPI mandate-limit routing, and immediate no-retry escalation for revoked mandates.
- **Communicates** with the customer via LLM-generated nudges (Standard + Hinglish
  tone) across SMS/WhatsApp/Email framing, and can send real email via Gmail SMTP.
- **Explains** every decision with a full audit trail and, for the additive ML layer,
  real SHAP feature-contribution breakdowns.
- **Verifies itself**: an internal correctness audit re-checks the system's own
  output against its stated business rules, and a 7-scenario adversarial chaos suite
  deliberately attacks the system to prove it holds up.
- **Predicts forward**: a Revenue-at-Risk engine flags active subscriptions likely to
  fail *before* they do.
- **Learns and experiments**: strategy performance is tracked per segment, drift is
  detected automatically, and an A/B experiment evaluator computes statistically
  honest results.
- **Extends beyond mandates**: the same orchestration core also runs
  checkout-abandonment recovery, B2B invoice chasing, and promise-to-pay tracking.

---

## 3. What's real vs simulated

This is the single most important section for anyone evaluating the project. Every API
response and UI element that shows a number carries a `SIMULATED` / `REAL_TEST` /
`ESTIMATE` discriminator, so a reviewer never has to guess.

| Capability | Status | Where it lives |
|---|---|---|
| Recovery decision logic (scoring, strategy, retry cap, compliance) | **Real** — deterministic code | `agent.py`, `scoring.py`, `salary_window.py` |
| Razorpay webhook signature verification | **Real** — HMAC-SHA256 over raw body per Razorpay spec | `razorpay_adapter.py`, `webhook_security.py` |
| Razorpay test-mode subscription/plan creation | **Real**, via Razorpay REST API in Test Mode | `razorpay_client.py`, `payment_executor.py` |
| Payment capture / recovery-link creation | **Real** in Test Mode when credentials are set; else simulated | `payment_executor.py` |
| Email delivery (OTP, dunning) | **Real** via Gmail SMTP when configured; logged `SIMULATED` provider otherwise | `email_service.py` |
| Merchant authentication | **Real** — PBKDF2-SHA256 hashing, hashed OTPs, server-side sessions | `auth.py` |
| The 180-case synthetic simulation | **Simulated by design** — fixed-seed dataset | `seed.py` |
| ML recovery-likelihood model | **Real model**, trained on simulation-derived data; additive, does *not* drive decisions | `backend/ml/` |
| Revenue-at-risk / forecast / economic value | **Estimates**, labeled `data_type: estimate` | `risk_engine.py`, `economic_value.py` |
| Channel EV / engagement rates | **Estimated** from industry baselines | `channel_engine.py` |
| Voice-channel recovery | **Interface only** — provider-ready, no real call placed | `channel_engine.py`, `multilingual.py` |
| Checkout / B2B / promise recovery | **Simulated** case data on the real orchestration core | `checkout_recovery.py`, `b2b_recovery.py`, `promise_tracker.py` |

---

## 4. Architecture

```
                    ┌─────────────────────────────┐
                    │   Real Razorpay Test Mode    │
                    │  (subscriptions, webhooks)   │
                    └──────────────┬───────────────┘
                                   │ X-Razorpay-Signature (HMAC-SHA256, raw body)
                                   ▼
┌──────────────┐        ┌──────────────────────┐
│  Synthetic   │        │  razorpay_adapter.py │
│  simulation  │        │  (real webhook path) │
│  (seed.py)   │        └──────────┬───────────┘
└──────┬───────┘                   │
       │       both shapes normalize to the SAME internal case record
       └────────────────┬──────────┘
                         ▼
              ┌─────────────────────┐
              │  RecoveryPipeline   │   agent.py
              │  1. DiagnosisAgent  │   classify, verify signature, dedup, validate
              │  2. TriageAgent     │   recoverability + health score, priority order
              │  3. StrategyAgent   │   per-reason strategy, retry cap, compliance
              │  4. CommunicationAgent│ LLM reasoning + nudge messages, dunning
              └──────────┬──────────┘
                         │
        ┌────────────────┼─────────────────────┐
        ▼                ▼                     ▼
┌───────────────┐ ┌──────────────┐   ┌────────────────────┐
│ audit_log     │ │ metrics.py   │   │ Self-verification  │
│ (append-only) │ │ (all figures)│   │ audit_check.py (7) │
│               │ │              │   │ chaos_test.py  (7) │
└───────────────┘ └──────────────┘   └────────────────────┘
```

**Additive layers alongside the core pipeline** (none drive agent decisions — they
inform, predict, or explain):

- **ML validation layer** (`backend/ml/`) — trained model + SHAP explainability.
- **Policy Sandbox** (`simulation_runner.py`) — Monte Carlo comparison of tunable
  policies against production, with 95% confidence intervals.
- **Revenue-at-Risk / Economic Value** (`risk_engine.py`, `economic_value.py`) —
  forward-looking prediction and expected-net-value optimization.
- **Closed learning loop** (`outcome_attribution.py`, `segment_learning.py`,
  `strategy_drift.py`, `adaptive_policy.py`, `experiment_evaluator.py`).
- **Recovery Orchestrator** (`recovery_orchestrator.py`) — a shared
  DETECT → PREDICT → INVESTIGATE → DECIDE → ACT → OBSERVE → MEASURE → LEARN pipeline
  reused by checkout, B2B, and promise-to-pay surfaces.

### Backend module map

| Area | Files |
|---|---|
| Web app / routing | `app.py`, `p7_routes.py`, `health.py` |
| Core 4-agent pipeline | `agent.py`, `scoring.py`, `salary_window.py`, `baseline.py` |
| Payment execution | `payment_executor.py`, `razorpay_client.py`, `razorpay_adapter.py` |
| Communication | `llm_client.py`, `messaging.py`, `multilingual.py`, `notifications.py`, `email_service.py` |
| ML layer | `ml/train_model.py`, `ml/predict.py`, `ml/explain.py`, `ml/generate_dataset.py` |
| Intelligence & forecasting | `intelligence.py`, `risk_engine.py`, `economic_value.py`, `anomaly_detector.py`, `degradation_investigator.py` |
| Learning loop | `outcome_attribution.py`, `segment_learning.py`, `strategy_drift.py`, `adaptive_policy.py`, `experimentation.py`, `experiment_evaluator.py` |
| Policy | `policy_engine.py`, `policy_center.py`, `simulation_runner.py` |
| Orchestrator surfaces | `recovery_orchestrator.py`, `checkout_recovery.py`, `b2b_recovery.py`, `promise_tracker.py`, `mandate_sequencer.py`, `channel_engine.py` |
| Verification | `audit_check.py`, `chaos_test.py` |
| Auth & security | `auth.py`, `security.py`, `webhook_security.py`, `rate_limit.py` |
| Infrastructure | `db.py`, `config.py`, `scheduler.py`, `seed.py`, `query.py`, `export.py`, `metrics.py`, `phase7_schema.py`, `demo_engine.py` |

---

## 5. The core recovery pipeline — the 4 agents

Lives in `agent.py` as `RecoveryPipeline`. Both a synthetic case and a real Razorpay
webhook normalize to the *same* internal case record, then flow through four agents in
order. The pipeline is deterministic (seeded RNG) so runs are reproducible.

### Agent 1 — DiagnosisAgent
Turns a raw webhook into a trusted, classified case.
- **Classifies** the event (`payment.failed`, `subscription.charged.failed`,
  `subscription.halted`) into an internal failure reason.
- **Verifies** the HMAC-SHA256 signature over the raw body; a missing/invalid signature
  is rejected and logged to the audit trail (`reject`) — it never enters the pipeline.
- **Validates input**: negative, zero, and non-finite amounts are rejected
  (`reject_invalid`) and excluded from every metric.
- **Deduplicates replays**: a valid event that was already processed is noted
  (`note_duplicate`) and ignored — never double-counted.

### Agent 2 — TriageAgent
Decides how much a case is worth pursuing and in what order.
- Computes a **0–100 recoverability score** (see §6) and logs it as a `score` event
  with a plain-English reasoning string.
- Computes a **subscription health score**.
- **Orders** cases highest-value-first so the highest-impact recoveries are handled
  first.

### Agent 3 — StrategyAgent
Selects and executes the reason-specific action under hard guardrails.
- **Per-reason strategy**: salary-window-timed retry (insufficient funds), fast silent
  retry (bank technical error), re-authorization link (mandate expired), or immediate
  escalation.
- **`mandate_revoked` is hard-gated**: it *never* retries — blocked before scoring even
  matters.
- **3-retry cap** enforced on every retry loop.
- **Mandate-limit routing**: an amount over the UPI mandate limit is routed through
  re-authorization instead of a normal retry.
- **RBI pre-debit notice**: a 24-hour pre-debit notification is issued before a retry;
  the compliance status is recorded per case.
- **Promise-to-pay sub-flow** (`maybe_promise`) captures a customer promise and tracks
  it.
- Retry execution goes through the `PaymentExecutionService` (see §7), in real
  test-mode or simulation depending on configuration.

### Agent 4 — CommunicationAgent
Narrates and nudges — but never decides.
- Generates the **reasoning narrative** and the **customer nudge message** via LLM
  (Groq/Llama), with a graceful template fallback if the LLM is unavailable.
- Runs the **3-stage dunning sequence** and sends re-auth links.
- **Critical boundary**: the LLM only *narrates* decisions already made by
  deterministic code. It never decides retries, escalations, or compliance status.

**Entry points**: `run_agent(policy, conn, seed)` runs the full batch;
`run_agent_traced()` streams each step for the live dashboard (SSE).
`PolicyParams` carries the tunable retry cap / scoring weights / salary-window mode used
by the Policy Sandbox without changing live defaults.

---

## 6. Recoverability scoring & salary-window timing

### Scoring (`scoring.py`)
A transparent, weighted **0–100** score. Higher means more worth pursuing. All weights
are named constants so they're easy to audit and tune:

| Factor | Weight | Meaning |
|---|---|---|
| `past_payment_success_rate` | 0.40 | Historical success (0–1) |
| `customer_tenure_months` | 0.20 | Capped at 24 months |
| `past_retry_count` | 0.20 | Retry burden (fewer prior retries scores higher) |
| `failure_reason` base | 0.20 | Per-reason recoverability prior |

Per-reason base recoverability: `bank_technical_error` 0.95 (usually transient),
`insufficient_funds` 0.70 (recoverable with timing), `mandate_expired` 0.55 (needs
re-auth), `mandate_revoked` 0.10 (rarely recoverable). `score_case()` returns the score
plus a factor breakdown; `explain_score()` produces the reasoning string logged with
every case. Weights and retry cap are overridable so the Policy Sandbox can test
sensitivity without touching live behavior.

### Salary-window inference (`salary_window.py`)
For insufficient-funds cases, retrying on the customer's actual payday recovers far more
than retrying blindly.
- **Generic fallback windows**: month start (days 1–3) and month end (days 25–31).
- **Per-customer inference**: when a case has at least 3 past successful-payment days
  (`history_success_days`), the modal payday is used to build a personalized ±1-day
  window, labeled `inferred (v2 personalization)`.
- **`generic_only` mode**: forces the generic window, used by the Policy Sandbox to
  measure the value of personalization by comparison.

### Baseline (`baseline.py`)
Simulates a naive "retry once, generic message" policy over the *same* seeded cases so
the agent-vs-baseline comparison on the dashboard is apples-to-apples.

---

## 7. Payment execution (real test-mode + simulation)

`payment_executor.py` — `PaymentExecutionService` executes recovery attempts.

- **Two modes** (`ExecutionMode`): `real_test` (Razorpay Test Mode) and `simulation`.
  The mode is resolved per case from its source and available credentials.
- **Real path**: attempts a payment capture, checks subscription status, verifies a
  subscription is inactive, or creates a recovery link — all via the Razorpay REST API
  in Test Mode.
- **Simulation path**: produces a deterministic, seeded outcome for synthetic cases.
- Every result becomes an audit entry (`ExecutionResult.to_audit_text`) tagged with its
  provenance, so outcomes are never confused between real and simulated.
- `verify_razorpay_credentials()` reports whether real execution is available.

`razorpay_client.py` handles subscription/plan creation and API calls;
`razorpay_adapter.py` handles the inbound real webhook path and its signature check.

### Intelligent Mandate Retry Sequencer (`mandate_sequencer.py`)
An adaptive "should we retry now?" engine used by the orchestrator surfaces. For a given
attempt it answers *why retry now, why not, why this channel, why stop* using signals:
max-retries reached, `mandate_revoked` (never), `mandate_expired` (blocked, re-auth
needed), salary-window timing for insufficient funds, a 2-hour fast retry for bank
errors, an expected-value gate, and exponential backoff on later attempts. Decisions and
signals are persisted to `mandate_retry_log` and labeled `ESTIMATED`.


## 8. Communication: LLM, messaging & multilingual


**LLM client (`llm_client.py`)** — wraps the Groq/Llama LLM used to generate the
reasoning narrative and customer nudge copy. If `GROQ_API_KEY` is unset or the call
fails, it falls back to deterministic templates — the app never hard-fails on a missing
LLM. The LLM only narrates; it never makes a recovery decision.

**Messaging (`messaging.py`)** — builds the customer-facing nudge messages (Standard +
Hinglish tone) framed for SMS / WhatsApp / Email, and drives the 3-stage dunning
sequence.

**Notifications (`notifications.py`)** — central notification dispatch that connects a
case action to a delivery channel and records what was (or would be) sent, honoring
per-merchant notification preferences.

**Multilingual (`multilingual.py`)** — structured multilingual recovery messaging for
**English, Hindi, and Hinglish**, with per-scenario templates (failed payment, failed
subscription, mandate retry, checkout abandonment, B2B receivable, promise-to-pay,
mandate expired, insufficient funds, bank technical error, escalation).
`generate_recovery_message()` picks the right template by reason/scenario + language;
`generate_all_languages()` returns all three for comparison. It also generates
**voice-call scripts** (`generate_voice_script`) that are marked `READY_FOR_PROVIDER` —
no real call is placed. Messages never include payment credentials or internal IDs.


---

## 9. The ML validation layer

Lives in `backend/ml/`. This is an **additive validation layer** — it predicts recovery
likelihood and explains predictions, but it does **not** drive any agent decision.

- **`generate_dataset.py`** — builds a training dataset from simulation-derived
  outcomes, with an explicit check against label leakage (no post-outcome feature is
  allowed into the feature set).
- **`train_model.py`** — trains and picks the best of LogisticRegression /
  GradientBoosting by ROC-AUC, saving `model.pkl` and `metrics.json` (confusion matrix,
  precision / recall / F1, ROC-AUC).
- **`predict.py`** — loads the model and returns a recovery probability per case, used
  to annotate cases in the dashboard.
- **`explain.py`** — real **SHAP** feature-contribution breakdowns per case ("why does
  the model predict this?" as signed contributions), verified for additivity
  (`base_value + Σ SHAP ≈ predict_proba`) to machine precision.

Exposed via `/api/ml/metrics` and `/api/ml/feature-importance`, and per-case via
`/api/case/<id>/explain`.

---

## 10. Forward-looking intelligence

These modules look forward and sideways rather than just reacting to a single failure.

**Revenue-at-Risk (`risk_engine.py`)** — scores active subscriptions on how likely they
are to fail *before* they do. `score_case_risk()` produces a per-case risk score
(normalized against the portfolio's p95 amount); `revenue_at_risk()` aggregates the
portfolio; `top_risks()` surfaces the highest-exposure cases. All outputs labeled
`estimate`.

**Economic Value (`economic_value.py`)** — expected-net-value optimization. For each
strategy it computes expected value = P(recovery) × amount − (strategy cost + customer
friction cost). `best_strategy_by_value()` picks the value-maximizing action,
`incremental_value()` measures lift over doing nothing, and `portfolio_ev()` rolls it up.
Labeled `estimate`.

**Anomaly detection (`anomaly_detector.py`)** — statistical alerts across the portfolio:
failure-rate spikes, escalation spikes, recovery-rate drops, retry exhaustion,
compliance degradation, and amount concentration, each with a z-score and severity.

**Degradation Investigator (`degradation_investigator.py`)** — root-cause analysis on
top of anomaly detection. `investigate()` answers questions like "why did recovery rate
drop?" or "which bank causes the most losses?" with observation → evidence → likely
cause → revenue impact → recommendation → confidence. All causal language is
deliberately **hedged** ("likely", "associated with", "strongest signal") — it never
claims proven causation without experimental evidence.

**Intelligence (`intelligence.py`)** — cross-cutting analytics: performance by failure
reason, by strategy outcome, by merchant category, failure rate by segment, incremental
revenue, strategy comparison (via repeated simulation runs), and per-merchant learning
summaries. Powers the `/api/intelligence/*` endpoints.


---

## 11. The closed learning loop

A disciplined loop: **outcome → attribution → segment-level performance → drift
detection → statistically honest evaluation → adaptive recommendation.**

**Outcome Attribution (`outcome_attribution.py`)** — the data backbone. For every
fully-resolved case it links the decision to its observed outcome and writes durable
`strategy_performance` records across multiple dimensions (global, per failure reason,
per merchant category). It's idempotent (keyed on customer_id) and every record carries
its **provenance**: `REAL_TEST`, `SIMULATION`, `HISTORICAL`, `ESTIMATE`, or `FORECAST`
— so only real test-mode outcomes are ever treated as real payment behavior.
`backfill_from_audit()` reconstructs performance from existing data;
`get_attribution_summary()` reports coverage and a provenance breakdown.

**Segment Learning (`segment_learning.py`)** — learns which strategy performs best for
which customer/case segment from the accumulated `strategy_performance` rows.
`best_strategy_for_case()` recommends a strategy for a specific case;
`strategy_ranking()` and `full_learning_summary()` expose the learned rankings.

**Strategy Drift (`strategy_drift.py`)** — detects when a previously strong strategy is
degrading. It compares a recent window (last 30 days by default) against the baseline
(all prior data); a relative recovery-rate drop above the threshold (15% default) raises
a `warning` (or `critical` above 30%) alert with a recommended investigation. Labeled
`actual`.


**Adaptive Policy (`adaptive_policy.py`)** — combines rule-based strategy selection with observed segment rates and a governance gate. `recommend_strategy()` proposes a strategy for a case backed by observed rates; the governance decision explains whether it may be auto-applied or needs human review. `batch_recommend()` and `policy_summary()` support portfolio-level use.

**Experimentation (`experimentation.py`)** — sets up A/B experiments with control and treatment arms across the case stream.

**Experiment Evaluator (`experiment_evaluator.py`)** — computes statistically honest A/B results, not just "arm A looked bigger." It calculates per-arm recovery rates, runs a **two-proportion z-test**, assigns a confidence label based on the z-score, sample sizes, and effect size, and estimates incremental revenue. `evaluate_experiment()` returns the full comparison with an overall data-type note reflecting the provenance mix of the underlying outcomes.

---

## 12. Policy Sandbox & experimentation

**Policy Engine (`policy_engine.py`)** — the definition of a tunable recovery policy: retry cap, scoring weights, and salary-window mode, plus validation of policy parameters.

**Simulation Runner (`simulation_runner.py`)** — the Monte Carlo comparison engine behind the Policy Sandbox. It runs a candidate policy against the production policy over many isolated in-memory databases (default 30 runs), and reports results as **mean ± 95% confidence interval** rather than a single lucky number. This lets a merchant see, for example, how changing the retry cap from 3 to 2 shifts the recovery rate — with honest uncertainty bounds. Exposed via `/api/simulate`.

---

## 13. Self-verification

The system re-checks its own work — this is a differentiator, not decoration.

**Correctness Audit (`audit_check.py`)** — independently re-derives every dashboard figure directly from `audit_log` and re-checks **7 business rules** against the live data (e.g. retry cap never exceeded, revoked mandates never retried, pre-debit notice present before retries, invalid amounts excluded from totals, replays not double-counted). It recomputes on demand — it is not a static report. A mismatch is treated as a bug. Exposed via `/api/audit-check`.

**Chaos Suite (`chaos_test.py`)** — **7 adversarial attack scenarios** run against isolated in-memory databases, never live data: replayed webhooks, negative/zero amounts, duplicate customer IDs, clock-skew timestamps, malformed LLM responses, webhook-signature edge cases, and a 2,000-case extreme-volume stress test. Three real bugs were found and fixed by this suite during development (input validation, replay dedup, a clock-skew compliance gap). Exposed via `/api/chaos-test`.

---

## 14. Beyond mandates — the Recovery Orchestrator (Phase 7)

`recovery_orchestrator.py` is a shared decision core reused by several revenue-recovery surfaces, so they aren't one-off features but instances of the same disciplined architecture. Its lifecycle: **DETECT → PREDICT → INVESTIGATE → DECIDE → ACT → OBSERVE → MEASURE → LEARN.** It exposes `create_case`, `detect_and_score` (risk score, recovery probability, priority), `decide_action` (action + channel + next slot under merchant policy, with an approval gate for high-value actions), `execute_action`, `record_outcome` (which feeds the learning loop), `measure_portfolio`, and `priority_queue`. Every case carries plain-English "what happened / why it matters / what's next".

The orchestrator supports these scenario types:

**Checkout Abandonment Recovery (`checkout_recovery.py`)** — registers abandoned checkouts, creates a recovery case with a time-limited recovery link (48h TTL), tracks the funnel (`recovery_funnel`), and marks sessions recovered. Stages: initiated → address entered → payment method selected → payment attempted → abandoned. Data is `SIMULATED` unless it came from a real Razorpay webhook.

**B2B Receivables Chaser (`b2b_recovery.py`)** — invoice lifecycle (due → reminded → overdue → follow-up → promised → escalated → paid / written-off) with intelligent priority scoring (amount + overdue days), reminders, escalation, `mark_paid`, and an aging-bucket summary (0–30 / 31–60 / 61–90 / 90+ days).

**Promise-to-Pay Tracker (`promise_tracker.py`)** — full promise lifecycle (upcoming → due today → paid / missed / broken → escalated / cancelled), automatic status refresh by date, and a conversion-rate summary. A promise made inside the mandate pipeline is linked back to its case.

**Channel Decisioning Engine (`channel_engine.py`)** — picks the optimal channel per case (email, SMS, WhatsApp, in-app, voice) by expected net value = P(recovery|channel) × amount − channel cost, using estimated per-channel cost and engagement baselines. Includes a **voice-ready abstraction**: it generates a call script and marks it `READY_FOR_PROVIDER`, but places no real call until a provider adapter (Exotel, Twilio, etc.) is plugged in; outcomes can only be `SIMULATED`.

**Policy Center (`policy_center.py`)** — per-merchant configurable recovery policy (max retries, cooldown, messages per week, preferred channel/language, working hours, min expected value, approval threshold, escalation thresholds, and feature toggles for checkout / B2B / voice). Validated and versioned, with reset-to-defaults.

**Demo Engine (`demo_engine.py`)** — a deterministic, isolated multi-step narrative for judge demonstrations that never touches real merchant data.

---

## 15. Merchant authentication & account management

`auth.py` provides real authentication (not a mock):

- **Register / OTP-verify / login / logout / forgot-password / reset-password / change-password / change-email.**
- **PBKDF2-SHA256** password hashing.
- **SHA-256-hashed OTPs** — the plaintext OTP is never stored or returned.
- **7-day server-side sessions** with a signed session cookie.
- Profile management, notification preferences, and a **security-events** feed.
- Rate limiting on auth endpoints (`rate_limit.py`).

Routes: `/api/auth/register`, `/verify-email`, `/resend-otp`, `/login`, `/logout`,
`/me`, `/forgot-password`, `/reset-password`; plus `/api/profile`,
`/api/change-email/*`, `/api/change-password/*`, `/api/notification-prefs`,
`/api/security-events`.

---

## 16. Email delivery

`email_service.py` delivers OTPs and recovery notifications through a clean provider
abstraction (`EmailProvider` → `GoogleSMTPProvider` / `SimulatedProvider`). With Gmail
SMTP configured it sends real email; with nothing configured it runs a fully logged
`SimulatedProvider` that never sends — so the app works end-to-end with zero email
config. `/api/send-test-email` verifies a live configuration.

---

## 17. Security

Implemented across `security.py`, `webhook_security.py`, `rate_limit.py`, and `app.py`.

- **Webhook signatures**: real Razorpay HMAC-SHA256 verified over the *raw request body* (not a re-serialized copy, which can silently break the signature), using `hmac.compare_digest` for constant-time comparison. **Fails closed**: missing or placeholder secrets are rejected, never silently allowed.
- **API-key gate** on every mutating endpoint (`/api/reset`, `/api/seed`, `/api/run-agent`, `/api/simulate`, etc.) via `X-API-Key`, checked in constant time. This is an explicitly documented single-shared-secret model appropriate for the project's scope; a production multi-tenant deployment would replace it with per-user authorization on the existing `auth.py` session layer.
- **PII masking** — customer IDs are masked in every UI surface and log line; the real value is used only for internal joins.
- **Insert-only audit log** — no `UPDATE` or per-row `DELETE` exists for `audit_log`; the only removal path is a full-table wipe during an explicit demo reset.
- **Replay protection** — webhook events are deduplicated by canonical identity; a replayed valid event is logged and ignored, never double-counted.
- **Input validation** — negative, zero, and non-finite amounts are rejected at ingestion and excluded from every total.
- **Security headers** on every response: `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, and a scoped Content-Security-Policy.
- **Correlation IDs** on every request for log tracing.
- **Rate limiting** (`rate_limit.py`) on sensitive endpoints.
- **SSE token gating** — the live agent stream is protected by a short-lived one-time token.

---

## 18. Data model

Storage is SQLite (`db.py` owns the schema and connection helpers; `phase7_schema.py` adds the orchestrator tables). Core tables:

- **`mandate_failures`** — one row per case: amount, failure reason, tenure, historical success rate, mandate limit, status, compliance status, dunning stage, source (synthetic / razorpay_live).
- **`audit_log`** — append-only event stream: every decision, retry, notification, escalation, and rejection, each with a plain-English reasoning string.
- **`recovery_jobs`** — scheduled/executed recovery attempts, with execution mode and outcome.
- **`merchants` / `sessions` / `otp_codes`** — authentication.
- **`strategy_performance`** — per-strategy, per-dimension counters with provenance (feeds the learning loop).
- **Phase-7 tables** — `recovery_cases`, `case_events`, `checkout_sessions`, `b2b_invoices`, `promises`, `channel_decisions`, `voice_scripts`, `mandate_retry_log`, `merchant_recovery_policies`, `approval_requests`, `payment_degradation_events`.

Every table is consistently labeled `REAL` / `SIMULATED` / `ESTIMATE` via a `data_type` / `source` / `is_demo` column so provenance is never ambiguous.

---

## 19. Frontend / dashboard

Served from `frontend/templates/` and `frontend/static/` (vanilla JS + CSS, no build
step). Key surfaces:

- **Core dashboard** — live 4-agent pipeline visualization (a real backend SSE stream, not a decorative timer), KPI cards, agent-vs-naive-baseline comparison, and cohort breakdowns (by tenure, by merchant category).
- **App shell** — sidebar navigation (Overview / Cases / Compliance / ML Insights / Policy Sandbox / Chaos Suite / Reports) and a ⌘K command palette with fuzzy case search, starring, and saved natural-language query views.
- **"Ask the data"** — natural-language queries translated to a **hardcoded field whitelist**, never to raw SQL from the LLM. The LLM's only job is intent → filter; the query always runs as parameterized SQL against real rows (`query.py`, `/api/ask`).
- **Compliance view** — RBI pre-debit badges, UPI mandate-limit routing, a rejected-webhooks panel, and the on-demand Correctness Audit.
- **ML Insights** — confusion matrix, precision/recall/F1, and per-case SHAP explanations.
- **Chaos Suite** — run all 7 adversarial scenarios live.
- **Policy Sandbox** — adjust retry cap / scoring weights / salary-window mode and run the Monte Carlo comparison.
- **Auth pages** — register, login, verify email, forgot password.

Static assets: `app.js` (dashboard), `auth.js`, `profile.js`, `p7.js` (orchestrator
surfaces), plus `style.css`, `dashboard.css`, `auth.css`, `reference-match.css`.

---

## 20. API reference

### Core pipeline & data (v1, `app.py`)

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/seed` | POST | Seed the 180-case synthetic dataset |
| `/api/run-agent` | POST | Run the full 4-agent pipeline |
| `/api/run-agent/stream` | GET | Live SSE stream of the pipeline (token-gated) |
| `/api/reset` | POST | Wipe demo data |
| `/api/status` | GET | Current run status |
| `/api/metrics` | GET | Portfolio KPIs (at-risk, recovered, rates) |
| `/api/cases` | GET | List cases (filterable) |
| `/api/case/<id>/audit` | GET | Full audit trail for a case |
| `/api/case/<id>/explain` | GET | ML/SHAP explanation for a case |
| `/api/case/<id>/replay` | POST | Re-run a single case |
| `/api/case/<id>/messages` | GET | Generated nudge messages |
| `/api/case/<id>/health` | GET | Subscription health score |
| `/api/cohorts`, `/api/analytics/timeseries` | GET | Cohort & time-series analytics |
| `/api/ask` | POST | Natural-language query (whitelisted) |
| `/api/export` | GET | Export cases/audit |
| `/api/webhook/razorpay` | POST | Real Razorpay webhook ingestion |
| `/api/rejected-webhooks`, `/api/webhook-events` | GET | Rejected / all webhook events |
| `/api/activity` | GET | Recent activity feed |

### Verification, ML & policy (v1)

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/audit-check` | GET | Run the 7-rule correctness audit |
| `/api/chaos-test` | GET | Run the 7 chaos scenarios |
| `/api/ml/metrics` | GET | Model confusion matrix + metrics |
| `/api/ml/feature-importance` | GET | Feature importance |
| `/api/simulate` | POST | Policy Sandbox Monte Carlo run |

### Intelligence, risk & learning (v1)

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/intelligence/*` | GET | By-reason / strategy / merchant / incremental analytics |
| `/api/risk/summary`, `/api/risk/case/<id>` | GET | Revenue-at-risk |
| `/api/ev/portfolio`, `/api/ev/case/<id>` | GET | Economic value |
| `/api/adaptive-policy/*` | GET | Strategy recommendations |
| `/api/anomalies`, `/api/investigate` | GET/POST | Anomaly & degradation investigation |
| `/api/learning/*` | GET/POST | Attribution, strategy performance, segments, drift, experiments, recommendations, policy versions & rollback, learning dashboard |
| `/api/scheduler/*` | GET/POST | Recovery-job scheduler management |
| `/api/execution/status`, `/api/execution/verify-credentials` | GET | Real-execution readiness |

### Recovery Orchestrator (v2, `p7_routes.py`)

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v2/portfolio` | GET | Unified portfolio measurement |
| `/api/v2/priority-queue` | GET | Highest-priority open cases |
| `/api/v2/cases` | GET/POST | List / create a recovery case |
| `/api/v2/cases/<id>` | GET | Case + timeline |
| `/api/v2/cases/<id>/score` | POST | Detect & score |
| `/api/v2/cases/<id>/decide` | POST | Decide action |
| `/api/v2/cases/<id>/execute` | POST | Execute action |
| `/api/v2/cases/<id>/outcome` | POST | Record outcome (feeds learning) |
| `/api/v2/cases/<id>/timeline` | GET | Case event timeline |
| `/api/v2/cases/<id>/message` | GET | Multilingual messages |
| `/api/v2/cases/<id>/voice-script` | POST | Generate voice script (no call) |
| `/api/v2/checkout/*` | GET/POST | Abandoned sessions, register, funnel, recover |
| `/api/v2/b2b/*` | GET/POST | Invoices, remind, escalate, paid, aging |
| `/api/v2/promises/*` | GET/POST | Promises, mark paid/missed, summary |
| `/api/v2/approvals`, `/api/v2/approvals/<id>` | GET/POST | Approval queue & decisions |
| `/api/v2/policy` | GET/PATCH | Merchant recovery policy |
| `/api/v2/policy/reset` | POST | Reset policy to defaults |
| `/api/v2/mandate-retry/schedule`, `/history` | POST/GET | Adaptive retry sequencer |
| `/api/v2/investigate`, `/api/v2/degradation/events` | GET | Revenue investigation |
| `/api/v2/channel/select` | POST | Channel decisioning |
| `/api/v2/demo/*` | GET/POST | Demo engine run/state/reset/seed |
| `/api/v2/analytics/*`, `/api/v2/revenue-journey` | GET | Funnels & revenue journey |
| `/api/v2/copilot/ask` | POST | Rule-based portfolio copilot |
| `/healthz` | GET | Liveness check (`health.py`) |

---

## 21. Metrics — how every number is computed

- **At risk** — sum of `amount` across all non-invalid, non-duplicate cases.
- **Recovered** — sum of `amount` for cases whose latest status is `recovered`.
- **Recovery / escalation rate** — computed from real `case_status`, never hardcoded.
- **Agent vs baseline** — the baseline simulates a naive "retry once, generic message" policy over the *same* seeded cases, so the comparison is apples-to-apples.
- Every one of these is independently re-derived by `audit_check.py` and compared against the live API response — a mismatch is treated as a bug, not rounded away.

`metrics.py` centralizes these computations so every surface reads the same numbers.

---

## 22. Testing

```bash
pytest                      # full suite
pytest --cov=backend        # with coverage
pytest -m slow              # include extreme-volume / long-running tests
```

The suite (`backend/tests/`, plus `conftest.py` / `pytest.ini` at the root) covers
unit-level logic (scoring, salary-window inference, policy parameterization), the full
webhook path (valid / invalid / missing / stale signatures, replay, malformed payloads),
integration paths (webhook → persistence → recovery → outcome), concurrency, and the
correctness-audit and chaos-suite rules ported into real `pytest` assertions. CI runs via
`.github/workflows/ci.yml`.

---

## 23. Running it locally

```bash
git clone <repo-url>
cd Mandate_Rescue
pip install -r requirements.txt
cp .env.example .env        # fill in what you have; the app runs in simulation mode with nothing set
python backend/app.py
```

Or with Docker:

```bash
docker compose up
```

Visit `http://localhost:5000`. On first load, register/login, then click **Seed demo
data** and **Run agent** to watch the pipeline work. A `Makefile` provides shortcuts for
common tasks.

---

## 24. Environment variables

Nothing is required to run the app — every missing credential degrades to an
explicitly-labeled simulation path, never a silent failure.

| Variable | Required? | Effect if unset |
|---|---|---|
| `GROQ_API_KEY` | No | Falls back to deterministic template reasoning/messages |
| `WEBHOOK_SECRET` | No | A random secret is generated for the synthetic webhook path; fails closed |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | No | Real test-mode subscription creation & capture disabled; simulation still works |
| `RAZORPAY_WEBHOOK_SECRET` | No | The real Razorpay webhook route rejects all events until set |
| `MANDATE_RESCUE_API_KEY` | No | A random per-process key is generated and logged; the UI picks it up |
| `SMTP_HOST` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `MAIL_FROM` | No | Email falls back to a `SimulatedProvider` that logs (never sends) |
| `FLASK_SECRET_KEY` | No | A random key is generated per process for session signing |
| `DRIFT_WINDOW_DAYS` / `DRIFT_THRESHOLD` / `DRIFT_MIN_SAMPLE` | No | Tune strategy-drift detection (defaults 30 / 0.15 / 5) |

Config resolution lives in `config.py`.

---

## 25. Demo flow (5 minutes)

1. **Seed → Run agent** — watch the real 4-agent pipeline process 180 cases live.
2. Open a **recovered case** — walk through its audit trail, LLM reasoning, and SHAP explanation.
3. **Ask the data**: "show me all non-compliant high-value cases" — a grounded query against real rows.
4. **Chaos Suite** — run it live; show all 7 adversarial scenarios defended.
5. **Policy Sandbox** — change the retry cap; show the Monte Carlo confidence-interval shift.
6. If real Razorpay credentials are set: show a real test-mode webhook arriving, its signature verifying, and flowing through the identical pipeline.
7. Close on the honest note: what's real, what's simulated, and why that distinction matters.

---

## 26. Known limitations

- The 180-case dataset is synthetic; real production volume/patterns aren't represented.
- The ML model is validated against outcomes generated by this project's own rule engine, not independent ground truth — treat its metrics as internal-consistency validation, not external accuracy proof.
- Scoring/health-score weights are hand-set, transparent constants, not calibrated against real historical data.
- The API-key gate is a single shared secret appropriate for this project's scope, not a multi-tenant production authorization model.
- Voice-channel recovery is an interface only; no real call is ever placed.
- Economic-value, channel-EV, and revenue-at-risk figures are estimates, labeled as such.
- No automated large-scale concurrency stress-testing beyond the included concurrency unit tests.

---

## 27. What we'd build next

- **Shadow-mode policy deployment** — run a candidate policy silently alongside production on the same live case stream (matched RNG, zero side effects), with a promotion gate requiring no compliance regression and a statistically significant recovery-rate improvement before going live.
- **A human-in-the-loop review queue** ("My Day") for high-value or long-tenure cases about to be escalated, so an ops analyst can approve, override, or snooze an agent decision — with the override logged as a first-class audit event.
- **Calibrate scoring weights** against real historical outcome data instead of hand-set constants.
- **Full multi-tenant authorization** on top of the existing `auth.py` session layer, replacing the single shared API key.
- **Real voice-provider adapter** (Exotel / Twilio) behind the existing voice-ready interface.
