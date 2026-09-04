# Mandate Rescue

[![CI](https://github.com/OWNER/Mandate_Rescue/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/Mandate_Rescue/actions/workflows/ci.yml)

> **AI-Powered Revenue Recovery OS for Merchants**  
> Detects failed recurring payments, diagnoses the root cause, selects the optimal recovery strategy, executes it, and learns what works — with real Razorpay Test Mode integration.

---

## The Problem

Merchants lose revenue because payments fail, subscriptions churn, and mandates expire. Generic retry logic wastes attempts on unrecoverable cases and misses the right window on recoverable ones.

- India's UPI Autopay and NACH e-mandate failure rate runs 15–25%
- Each failure is a recovery decision with a cost, a probability, and an optimal window
- Most systems retry blindly — Mandate Rescue diagnoses, decides, and learns

---

## The Solution: The Recovery Loop

```
  PREDICT          INVESTIGATE        DECIDE          ACT
  Revenue at risk  →  Root cause    →  Strategy     →  Razorpay Test Mode
  Risk scoring        Failure type     Rule-based       Payment link
  Anomaly alerts      Health score     + AI-driven      Retry scheduling

      ↑                                                      ↓
  OPTIMIZE         LEARN              OBSERVE         MEASURE
  Policy versioning ←  Segment       ←  Recovery     ←  Outcome tracking
  A/B experiments      learning         outcomes         Audit trail
  Drift detection      Recommendations  State machine    Economic value
```

---

## Architecture

```
Razorpay / Payment Events
          ↓
 ┌─────────────────────────┐
 │   Event & Webhook Layer  │  HMAC-SHA256 sig verify · idempotency · lifecycle tracking
 └───────────┬─────────────┘
             ↓
 ┌─────────────────────────┐
 │   Risk + Intelligence    │  Risk scoring · anomaly detection · revenue-at-risk prediction
 └───────────┬─────────────┘
             ↓
 ┌─────────────────────────┐
 │  Recovery Decision Engine│  4-agent pipeline · strategy selection · compliance gates
 └───────────┬─────────────┘
             ↓
 ┌─────────────────────────┐
 │ Execution / Communication│  Razorpay Test API · payment links · dunning messages
 └───────────┬─────────────┘
             ↓
 ┌─────────────────────────┐
 │    Outcome Tracking      │  Append-only audit trail · state machine · job queue
 └───────────┬─────────────┘
             ↓
 ┌─────────────────────────┐
 │   Analytics + Learning   │  Segment learning · policy versioning · A/B experiments
 └─────────────────────────┘
```

---

## ⚠ REAL vs SIMULATED vs ESTIMATED — Read This First

This distinction is critical. Nothing in this project is mislabeled.

### REAL (no fabrication)

| Feature | Evidence |
|---|---|
| Razorpay webhook signature verification | HMAC-SHA256 over raw body bytes · `razorpay_adapter.py` |
| Webhook reception and processing | `POST /api/webhooks/razorpay` · Flask route · `app.py` |
| Webhook lifecycle tracking | RECEIVED→VERIFIED→PERSISTED→QUEUED→COMPLETED · `webhook_events` table |
| Idempotency / duplicate-event protection | `webhook_events.razorpay_event_id UNIQUE` constraint |
| Recovery job queue | Durable `recovery_jobs` table · idempotency key · scheduler worker |
| Razorpay Test Mode API calls | `razorpay_client.py` — plan, subscription, payment link, payment capture |
| Merchant auth system | OTP email verification · session cookies · security events |
| Rule-based recovery pipeline | 4-agent deterministic pipeline — Diagnosis, Triage, Strategy, Communication |
| Audit trail | Append-only `audit_log` — every decision, every state transition |
| ML model metrics | Trained on synthetic data; evaluated on held-out test set; numbers from `metrics.json` |

### SIMULATED / DEMO (always clearly labelled)

| Feature | Label |
|---|---|
| 180-case synthetic seed (scale demo) | `source = 'synthetic'` |
| UPI debit attempt outcomes | `ExecutionMode.SIMULATION` — Razorpay Test Mode has no `POST /subscriptions/{id}/charge` |
| Monte Carlo policy sandbox | `data_type: "simulation"` |
| Chaos test scenarios | Isolated in-memory DBs — never touch production data |
| Benchmark comparisons | Monte Carlo RNG with 30-run Student's t-CI |

### ESTIMATED (model-based, clearly labelled)

| Feature | Label |
|---|---|
| Revenue-at-risk scores | `data_type: "estimate"` |
| Recovery probability (ML model) | Informational only — never drives agent decisions |
| Economic value per intervention | `data_type: "estimate"` |
| Revenue projections | `[ESTIMATE — simulation-based counterfactual]` |

---

## 2-Minute Judge Demo

**Prerequisite:** Server running, 180 cases seeded (see Quick Start below).

| Step | Action | What you see | REAL or SIMULATED |
|---|---|---|---|
| 1 | Log in as merchant | Dashboard overview | REAL — auth system |
| 2 | Overview tab | Revenue at risk, recovery rate, anomaly alerts | REAL — computed from DB rows |
| 3 | Send test webhook | `python scripts/send_test_razorpay_webhook.py` | REAL — signature verified, case created |
| 4 | Cases tab | New case with `razorpay_live` source badge | REAL — from real webhook |
| 5 | Click case → Audit tab | Webhook lifecycle: RECEIVED→VERIFIED→PERSISTED→QUEUED | REAL — DB tracking |
| 6 | Run agent | Strategy selected, messages generated, recovery scheduled | REAL pipeline + SIMULATED debit outcome |
| 7 | Case replay | Step-by-step diagnosis → score → strategy → outcome | REAL — stored audit trail |
| 8 | Analytics tab | Recovery by failure reason, strategy performance | REAL — from `audit_log` rows |
| 9 | Learning tab | Segment insights, policy recommendations | REAL (on synthetic data) |
| 10 | Ask the data | "Which failure type has the worst recovery rate?" | REAL — parameterized SQL |

---

## Track 03 Capability Map

Razorpay Track 03: AI-powered revenue recovery for failed recurring payments.

| Capability | Status | Implementation |
|---|---|---|
| Payment degradation → root cause → recovery action | ✅ Implemented | `DiagnosisAgent` → `StrategyAgent` → `payment_executor.py` |
| Failed-subscription recovery | ✅ Implemented | `subscription.charged.failed` / `subscription.halted` events mapped to recovery pipeline |
| Mandate retry sequencing | ✅ Implemented | `recovery_jobs` table · salary-window scheduling · RBI 24h pre-debit gate |
| Intelligent recovery orchestration | ✅ Implemented | 4-agent pipeline · adaptive policy · economic value calculation |
| Checkout drop-off recovery | ⚠️ Partial | Payment Link flow recovers expired mandates; no full checkout session tracking |
| B2B receivables chaser | ⚠️ Partial | Invoice/utility category supported in seed + messaging; no dedicated B2B workflow |
| Hinglish / multichannel recovery | ✅ Implemented | `messaging.py` — Standard + Hinglish variants per failure reason |
| Promise-to-pay tracking | ✅ Implemented | `case_status = 'promised'` · `broken_promise` state · state machine in `db.py` |
| Outcome learning / closed loop | ✅ Implemented | `strategy_performance` · `segment_learning.py` · `policy_engine.py` · A/B experiments |
| Anomaly detection | ✅ Implemented | 6 statistical detectors — failure spike, escalation surge, recovery drop, compliance degradation |
| Explainable decisions | ✅ Implemented | SHAP per-case · risk factors · adaptive policy `explain` trace · audit trail |

---

## Quick Start

### 1. Install

```bash
git clone <repo>
cd Mandate_Rescue
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Minimum required — generate fresh secrets:

```bash
# Fail-closed: the app rejects all synthetic webhooks without this
python -c "import secrets; print('WEBHOOK_SECRET=' + secrets.token_hex(32))"

# Fail-closed: the app rejects all real Razorpay webhooks without this
# Get from: Razorpay Dashboard → Settings → Webhooks → Secret
# RAZORPAY_WEBHOOK_SECRET=<from dashboard>

# Optional — real Razorpay Test Mode execution
# Get from: Razorpay Dashboard → Settings → API Keys → Test Mode
# RAZORPAY_KEY_ID=rzp_test_...
# RAZORPAY_KEY_SECRET=...

# Optional — LLM narration (falls back to templates without this)
# GROQ_API_KEY=gsk_...
```

### 3. Train the ML model (optional, ~5 seconds)

```bash
python backend/ml/train_model.py
```

The app runs without the model — the ML column shows `—` until training runs.

### 4. Run

```bash
python backend/app.py
```

Open http://127.0.0.1:5000 — log in, then:

1. Click **Reset demo** — seeds 180 synthetic cases
2. Click **Run agent** — processes all cases through the 4-agent pipeline
3. Explore **Overview** — KPIs, recovery funnel, anomaly alerts, at-risk revenue
4. Check **Analytics** — strategy performance, investigator, time-series
5. Click any case — full audit timeline + execution panel

---

## Docker

```bash
cp .env.example .env      # fill in WEBHOOK_SECRET at minimum
touch mandate_rescue.db
docker compose up --build
```

Opens on http://127.0.0.1:5000. SQLite is bind-mounted so data survives restarts.

---

## Real Razorpay Test Mode Verification

### Sending a test webhook (exercises the real signature path)

```bash
# Set RAZORPAY_WEBHOOK_SECRET in .env to any strong random value.
# Start the server, then:
python scripts/send_test_razorpay_webhook.py \
    --url http://127.0.0.1:5000 \
    --customer-id CUSTDEMO1 \
    --amount 2500 \
    --event payment.failed

# Response:
# POST http://127.0.0.1:5000/api/webhooks/razorpay
#   event=payment.failed  customer_id=CUSTDEMO1  amount=Rs2500.0
#   -> 200 { "ok": true, "lifecycle": "QUEUED", "created": true, "jobs_queued": 1 }
```

This sends a payload signed with **the exact same HMAC-SHA256-over-raw-body scheme** that Razorpay's own infrastructure uses, so it exercises the real verification path end-to-end.

### Running real integration tests

```bash
# Tests that do NOT require credentials (run always)
pytest backend/tests/test_razorpay_adapter.py backend/tests/test_razorpay_webhook_route.py -v

# Tests that require real Razorpay Test Mode credentials
RZP_INTEGRATION=1 \
RAZORPAY_KEY_ID=rzp_test_... \
RAZORPAY_KEY_SECRET=... \
pytest backend/tests/test_phase4_integration.py -k "real" -v
```

### Known Test Mode limitation

Razorpay Test Mode does **not** expose `POST /subscriptions/{id}/charge`. There is no API to programmatically trigger an out-of-cycle UPI debit attempt. This limitation is documented in `payment_executor.py`, shown in the dashboard, and clearly labeled wherever it appears. The real execution path for expired/revoked mandates uses Payment Links — customer-driven, real Razorpay API call.

Full report: [`docs/evidence/RAZORPAY_VERIFICATION_REPORT.md`](docs/evidence/RAZORPAY_VERIFICATION_REPORT.md)

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest                          # 654 tests, 0 failures
pytest -q --tb=short            # compact output
```

### What the tests cover

| Area | Test file(s) | Count |
|---|---|---|
| Razorpay signature verification | `test_razorpay_adapter.py` | 11 |
| Webhook route end-to-end | `test_razorpay_webhook_route.py` | 5 |
| Idempotency / duplicate protection | `test_idempotency.py` | 3 |
| Webhook security (fail-closed secrets) | `test_webhook_security.py` | ~8 |
| Authentication system | `test_auth_system.py` | ~25 |
| Recovery pipeline | `test_audit_invariants.py`, `test_chaos_suite.py` | ~30 |
| ML model loading / inference | `test_adaptive_policy.py` | 13 |
| Scoring engine | `test_scoring.py` | 18 |
| Payment executor | `test_payment_executor.py` | 24 |
| Scheduler / job queue | `test_scheduler.py` | 22 |
| Phase 4 integration | `test_phase4_integration.py` | 12 |
| Revenue intelligence | `test_intelligence.py`, `test_risk_engine.py`, `test_economic_value.py`, `test_anomaly_detector.py` | 62 |
| Concurrency / chaos | `test_concurrency.py`, `test_chaos_suite.py` | ~20 |
| Benchmark | `test_benchmark.py`, `test_performance_p5.py` | ~15 |
| Phase 6.5 regression | `test_phase65_regression.py` | 54 |
| Everything else | 15 more test files | ~282 |

---

## Technical Architecture

### Backend modules

| Module | Responsibility |
|---|---|
| `app.py` | Flask application — 50+ routes, SSE, security middleware, auth |
| `agent.py` | 4-agent recovery pipeline: Diagnosis, Triage, Strategy, Communication |
| `db.py` | SQLite data-access layer — schema, FSM, migrations, idempotency |
| `seed.py` | 180-record synthetic case generator (seed=42, deterministic) |
| `scoring.py` | Recoverability score 0–100 (weighted formula) |
| `salary_window.py` | Salary-window inference for retry timing (RBI compliance) |
| `messaging.py` | Template nudges — Standard + Hinglish per failure reason |
| `llm_client.py` | LLM wrapper — Groq → NVIDIA → OpenAI chain, template fallback |
| `query.py` | NL query → parameterized SQL (closed field whitelist, never LLM-generated SQL) |
| `metrics.py` | KPI aggregations, JOIN queries, Wilson CIs |
| `baseline.py` | Naive + dumb-persistence baseline comparisons |
| `health.py` | Subscription health score |
| `security.py` | API-key gate |
| `webhook_security.py` | Fail-closed HMAC for synthetic pipeline |
| `razorpay_adapter.py` | Real Razorpay webhook verification + payload mapping |
| `razorpay_client.py` | Test Mode API client — plans, subscriptions, payments, links |
| `payment_executor.py` | REAL_TEST / SIMULATION execution service |
| `scheduler.py` | Durable job queue worker — `BEGIN IMMEDIATE`, stale-job reset |
| `intelligence.py` | Strategy/failure-reason/merchant analytics |
| `risk_engine.py` | Revenue-at-risk prediction with contributing factors |
| `adaptive_policy.py` | Data-driven strategy recommendation + governance tiers |
| `economic_value.py` | E[net_value] per intervention |
| `anomaly_detector.py` | 6 statistical anomaly detectors |
| `simulation_runner.py` | Monte Carlo policy sandbox |
| `auth.py` | Merchant auth — OTP, sessions, security events |
| `email_service.py` | Simulated / Google SMTP email delivery |
| `rate_limit.py` | In-process sliding-window rate limiter |
| `config.py` | Centralized env-var management with startup validation |
| `notifications.py` | Notification abstraction — Demo / Log / extensible provider adapters |
| `outcome_attribution.py` | Writes strategy_performance with provenance tags |
| `segment_learning.py` | Fallback hierarchy: merchant → failure_reason → global → rule-based |
| `policy_engine.py` | Evidence-gated recommendations + policy version management |
| `strategy_drift.py` | Detects strategy performance degradation over time |
| `experiment_evaluator.py` | Two-proportion z-test for A/B experiment evaluation |
| `ml/train_model.py` | LR vs GBM training on stratified 80/20 split |
| `ml/predict.py` | Batch-optimized inference + background warm-up |
| `ml/explain.py` | SHAP per-case + global importance |
| `audit_check.py` | 7-rule correctness audit (read-only) |
| `chaos_test.py` | 10 adversarial scenarios (isolated in-memory DBs) |

### Database schema (key tables)

```
mandate_failures            Primary case store (PK: customer_id)
audit_log                   Append-only decision trail (AUTOINCREMENT)
webhook_events              Idempotency table (razorpay_event_id UNIQUE)
state_transitions           FSM history (FK → mandate_failures)
recovery_jobs               Durable job queue (idempotency_key UNIQUE)
strategy_performance        Per-dimension strategy stats with provenance
experiments                 A/B experiment definitions
experiment_assignments      Case-to-arm mapping (deterministic, immutable)
experiment_outcomes         Final outcome per case (write-once)
policy_versions             Immutable version history (DRAFT→ACTIVE)
policy_recommendations      Evidence-gated recommendations
policy_audit_log            Append-only governance record
merchants / sessions / otps Auth tables
```

### Frontend

Flask-served Jinja2 templates + vanilla JS SPA. 9 views: Overview, Cases, Analytics, Learning, Webhook Inspector, Case Replay, Scheduler, Execution, Profile.

### Security

| Area | Implementation |
|---|---|
| Razorpay webhook auth | HMAC-SHA256 over raw body bytes, constant-time compare, fail-closed |
| Synthetic webhook auth | HMAC-SHA256 over canonical string, same fail-closed pattern |
| Placeholder secret detection | `_INSECURE_PLACEHOLDERS` frozenset — any known bad value is rejected |
| Mutating endpoint gate | `X-API-Key` header required on all state-changing routes |
| SSE stream auth | One-use 60-second token (browser EventSource cannot send custom headers) |
| Merchant auth | Bcrypt passwords, OTP email verification, 7-day session cookies |
| SQL injection | Parameterized queries throughout — no string interpolation of user input |
| NL query injection | LLM output filtered via hardcoded field whitelist before SQL |
| Security headers | CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy |
| Rate limiting | Sliding-window per IP on LLM + agent endpoints |
| Correlation IDs | Per-request `X-Correlation-ID` in all logs and responses |

---

## Performance (180 cases, warm process)

| Operation | Latency | Note |
|---|---|---|
| `/api/cases` (180 rows, with ML scores) | 13 ms | Batch ML inference + single JOIN |
| Webhook HTTP response | < 10 ms | Async — persists + enqueues, does not run pipeline |
| `/api/exceptions` | 0.9 ms | JOIN query (was N+1 before Phase 5) |
| `/api/activity` | 0.5 ms | DESC LIMIT 40 (was full-table scan) |
| `risk_engine.revenue_at_risk` | 2.7 ms | |
| `intelligence.full_summary` | 46 ms | 6 aggregates, single call |
| `anomaly_detector` (6 detectors) | 10 ms | |
| ML model cold-start | eliminated | Background thread warm-up at import time |

---

## Data Provenance Labels

Every metric in this system carries an explicit label. Nothing is unmarked.

| Label | Meaning |
|---|---|
| `actual` | Computed from real `mandate_failures` / `audit_log` rows |
| `estimate` | Derived from the probability model or configurable parameters |
| `simulation` | Monte Carlo RNG — Policy Sandbox, strategy comparison |
| `mixed` | Response contains both actual and estimated fields — each field individually labelled |
| `REAL_TEST` | source=razorpay_live AND execution_mode=real_test |
| `SIMULATION` | execution_mode=simulation |
| `HISTORICAL` | Pre-Phase-4 agent pipeline run |

---

## ML Validation Layer

```bash
python backend/ml/train_model.py   # ~5 seconds; writes model.pkl + metrics.json
```

- Trains LogisticRegression vs GradientBoostingClassifier on stratified 80/20 split
- Winner selected by ROC-AUC (LogisticRegression: 0.8964)
- SHAP values per case + global feature importance
- **Non-decision:** model predictions are informational only — the agent never consults them
- **Synthetic training data:** labels come from the rule-based pipeline's simulation runs, not real payments — stated explicitly throughout

Dependencies: sklearn 1.9.0 · joblib 1.6.0 · numpy 2.5.2 (no version warning on model load).  
Full rationale: [`docs/evidence/SKLEARN_VERSION_STRATEGY.md`](docs/evidence/SKLEARN_VERSION_STRATEGY.md)

---

## Known Limitations

| Area | Limitation |
|---|---|
| UPI debit trigger | No `POST /subscriptions/{id}/charge` in Razorpay Test Mode — debit outcomes labeled SIMULATION |
| Message delivery | Generated but not sent — DemoAdapter only; real SMS/WhatsApp requires Twilio/SNS integration |
| ML training data | Synthetic labels from simulation — not real payment outcomes |
| Multi-worker | SQLite single-writer; `busy_timeout=15s` + `BEGIN IMMEDIATE` handles brief contention |
| Rate limiting | In-process state — does not share limits across multiple Gunicorn workers |
| Auth scope | Single-tenant; policy engine operates on global namespace (merchant_category = "all") |

---

## Project Structure

```
Mandate_Rescue/
├── backend/
│   ├── app.py                   Flask app — 50+ routes, SSE, security, auth
│   ├── agent.py                 4-agent recovery pipeline
│   ├── db.py                    SQLite DAL, FSM, migrations, idempotency
│   ├── razorpay_adapter.py      Real Razorpay webhook verification + mapping
│   ├── razorpay_client.py       Test Mode API client
│   ├── payment_executor.py      REAL_TEST / SIMULATION execution service
│   ├── scheduler.py             Durable job queue worker
│   ├── ml/                      Additive ML validation layer
│   │   ├── train_model.py       LR vs GBM, stratified split
│   │   ├── predict.py           Batch inference + background warm-up
│   │   └── explain.py           SHAP explainability
│   └── tests/                   654 tests across 30+ test files
├── frontend/
│   ├── templates/               Jinja2 HTML (SPA: 9 views)
│   └── static/                  Vanilla JS + CSS
├── scripts/
│   └── send_test_razorpay_webhook.py   Demo/test webhook sender
├── docs/evidence/
│   ├── RAZORPAY_VERIFICATION_REPORT.md
│   └── SKLEARN_VERSION_STRATEGY.md
├── Dockerfile                   python:3.12-slim, Gunicorn, non-root, tini
├── docker-compose.yml
├── requirements.txt             Pinned to sklearn 1.9.0 / joblib 1.6.0 / numpy 2.5.2
└── .env.example                 All required env vars documented, no hardcoded secrets
```

---

## Phase 7 — Revenue Recovery OS (COMPLETE)

Phase 7 transforms Mandate Rescue into a full **AI-Powered Revenue Recovery OS**.

### New in Phase 7

| Capability | Status |
|---|---|
| Unified Recovery Case model (7 scenario types) | COMPLETE |
| Recovery Orchestrator: DETECT→PREDICT→DECIDE→ACT→OBSERVE→MEASURE→LEARN | COMPLETE |
| Checkout Abandonment Recovery | COMPLETE |
| B2B Receivables Chaser + Invoice Aging | COMPLETE |
| Promise-to-Pay Tracker | COMPLETE |
| Intelligent Mandate Retry Sequencer (adaptive timing) | COMPLETE |
| Payment Degradation Investigator + Root Cause | COMPLETE |
| Hinglish + Multilingual Recovery Messaging | COMPLETE |
| Multichannel Decisioning (Email, SMS, WhatsApp-ready, In-app) | COMPLETE |
| Voice-Ready Script Generation | COMPLETE |
| Merchant Command Center (KPIs + Priority Queue) | COMPLETE |
| Revenue Journey Visualization | COMPLETE |
| Unified Case View (timeline, actions, outcome) | COMPLETE |
| Merchant Copilot AI Assistant | COMPLETE |
| Policy Center (per-merchant configurable) | COMPLETE |
| Human-in-the-Loop Approval Flow | COMPLETE |
| Recovery Analytics (funnel, aging, promise conversion) | COMPLETE |
| Demo Mode (deterministic, isolated from real data) | COMPLETE |
| Adaptive Learning (strategy performance feed-back) | COMPLETE |
| Merchant Data Isolation (merchant_id scoping on all tables) | COMPLETE |

### Test results (Phase 7)

`
714 passed  2 skipped  0 failures  0 warnings
Previous baseline: 655  (+59 new passing tests)
`

---

## Roadmap (post-Phase 7)

- Replace SQLite with PostgreSQL for multi-worker production deployment
- Real UPI debit trigger when Razorpay exposes a suitable Test Mode API
- SMS/WhatsApp delivery via Razorpay Messages or Exotel integration
- Real ML training data from anonymised historical outcomes
- OpenTelemetry distributed tracing
