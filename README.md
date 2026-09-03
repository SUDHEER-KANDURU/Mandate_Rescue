# Mandate Rescue

[![CI](https://github.com/OWNER/Mandate_Rescue/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/Mandate_Rescue/actions/workflows/ci.yml)

> **An adaptive revenue protection engine** for UPI Autopay and NACH e-mandate failures.  
> Detects, diagnoses, triages, recovers, audits, and predicts — with full Razorpay Test Mode integration and explainable intelligence at every step.

---

## What it does

When a recurring payment fails, Mandate Rescue:

1. **Receives** the Razorpay webhook (real HMAC-verified) or synthetic seed
2. **Diagnoses** the failure reason (insufficient funds, expired, revoked, bank error)
3. **Scores** each case 0–100 on recoverability using customer history
4. **Triages** highest-value cases first
5. **Selects** the optimal strategy per case and per merchant category (data-driven)
6. **Schedules** retry jobs with RBI-compliant 24h pre-debit notifications
7. **Executes** via Razorpay Test Mode (payment links, subscription checks) or simulation
8. **Tracks** every decision in an append-only audit trail with full state machine enforcement
9. **Predicts** at-risk revenue before failures occur, with contributing factors
10. **Detects** anomalies: failure spikes, escalation surges, compliance degradation
11. **Answers** analytical questions: "Why did recovery fall?", "Which strategy performs best?"
12. **Calculates** expected net value of each intervention and incremental revenue over baselines

---

## Architecture

```
Razorpay Webhook (real / test-mode)   OR   Synthetic 180-case seed
          │                                          │
          ▼                                          ▼
 ┌─────────────────────────────────────────────────────────────┐
 │                    Webhook Gateway                          │
 │  • HMAC-SHA256 signature (raw body bytes, constant-time)    │
 │  • Idempotency (webhook_events.razorpay_event_id UNIQUE)    │
 │  • Amount validation (> 0, finite, ≤ Rs 1 crore)           │
 │  • Event persistence → mandate_failures                     │
 └──────────────────────────┬──────────────────────────────────┘
                            │
                            ▼
 ┌─────────────────────────────────────────────────────────────┐
 │                 Recovery Orchestrator                       │
 │                                                             │
 │  DiagnosisAgent → TriageAgent → StrategyAgent               │
 │       │               │               │                     │
 │  Classify + verify  Score (0-100)   Per-reason strategy     │
 │  failure reason     Health score    Retry cap / RBI gate    │
 │                     Triage order    Dunning sequence        │
 │                                           │                 │
 │                                    CommunicationAgent       │
 │                                    LLM narration (optional) │
 └──────────────────────────┬──────────────────────────────────┘
                            │
           ┌────────────────┼───────────────┐
           ▼                ▼               ▼
     Recovered          Escalated       Rejected
           │
           ▼
 ┌─────────────────────────────────────────────────────────────┐
 │  Phase 4: Real Execution Layer                              │
 │  PaymentExecutionService (REAL_TEST | SIMULATION)           │
 │  → Razorpay Test API: capture payment / create payment link │
 │  → Durable recovery_jobs table (idempotency_key UNIQUE)     │
 │  → Scheduler worker (BEGIN IMMEDIATE claim)                 │
 └──────────────────────────┬──────────────────────────────────┘
                            │
                            ▼
 ┌─────────────────────────────────────────────────────────────┐
 │  Phase 5: Adaptive Revenue Intelligence                     │
 │  • Revenue-at-risk prediction (risk_engine)                 │
 │  • Data-driven strategy selection (adaptive_policy)         │
 │  • Expected net value calculation (economic_value)          │
 │  • Anomaly detection (6 detectors, statistical)             │
 │  • Revenue Investigator (analytical Q&A from real data)     │
 │  • Performance: batch ML inference, JOIN queries, indexes   │
 └─────────────────────────────────────────────────────────────┘
```

---

## Phases completed

| Phase | Description | Key deliverables |
|---|---|---|
| **1** | Core pipeline | DiagnosisAgent, TriageAgent, StrategyAgent, CommunicationAgent, synthetic 180-case seed, audit trail, state machine |
| **2** | Hardening | Idempotency, concurrency protection, RBI compliance, correctness audit (7 rules), chaos suite (10 adversarial scenarios) |
| **3** | Intelligence | ML validation layer (LogisticRegression vs GBM + SHAP), Policy Sandbox (Monte Carlo), baselines, Case Replay, Webhook Inspector |
| **4** | Real execution | Razorpay Test Mode integration, `payment_executor.py` (REAL_TEST/SIMULATION modes), `scheduler.py` (durable job queue, `BEGIN IMMEDIATE`), `recovery_jobs` table, stale-job restart safety |
| **5** | Revenue intelligence + performance | `intelligence.py`, `risk_engine.py`, `adaptive_policy.py`, `economic_value.py`, `anomaly_detector.py`, Revenue Investigator (`/api/investigate`), batch ML prediction (6500ms → 13ms), JOIN queries (N+1 fixed), 5 DB indexes added, Analytics view |

**Test count:** 463 passing, 2 skipped (real-API opt-in), 0 failures

---

## Running locally

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

Edit `.env`:

```env
# Required — fail-closed, no default
WEBHOOK_SECRET=<python -c "import secrets; print(secrets.token_hex(32))">

# Required for real Razorpay webhook intake
RAZORPAY_WEBHOOK_SECRET=<from Razorpay Dashboard → Settings → Webhooks>

# Optional — real Razorpay Test Mode execution (payment links, subscription checks)
RAZORPAY_KEY_ID=rzp_test_...
RAZORPAY_KEY_SECRET=...

# Optional — LLM narration (falls back to templates without this)
GROQ_API_KEY=gsk_...
# Keep LLM_LIVE_TOP_N low (default 5) to stay within Groq free-tier 8K TPM limit
# LLM_LIVE_TOP_N=5

# Optional — auto-generated on startup if omitted
MANDATE_RESCUE_API_KEY=<python -c "import secrets; print(secrets.token_urlsafe(32))">
```

### 3. Train the ML model (optional but recommended)

```bash
python backend/ml/train_model.py
```

This writes `backend/ml/model.pkl` and `backend/ml/metrics.json`. The app works without it (ML column shows `—`). Takes ~5 seconds on a laptop.

### 4. Run

```bash
python backend/app.py
```

Open http://127.0.0.1:5000.

**Quick start:**
1. Click **Reset demo** — seeds 180 synthetic cases
2. Click **Run agent** — processes all cases through the 4-agent pipeline
3. Explore **Overview** — KPIs, recovery funnel, anomaly alerts, at-risk revenue
4. Visit **Analytics** — by-failure-reason breakdown, strategy performance, investigator
5. Click any case row — see the full audit timeline + execution panel

---

## Docker

```bash
cp .env.example .env   # fill in WEBHOOK_SECRET at minimum
touch mandate_rescue.db
docker compose up --build
```

Opens on http://127.0.0.1:5000. SQLite is bind-mounted (`./mandate_rescue.db`) so data survives restarts.

---

## API reference

### Core pipeline

| Method | Route | Auth | Description |
|---|---|---|---|
| `GET` | `/` | — | Dashboard SPA |
| `POST` | `/api/seed` | ✓ | Re-seed 180 synthetic cases |
| `POST` | `/api/reset` | ✓ | Reset DB + re-seed + clear LLM cache |
| `POST` | `/api/run-agent` | ✓ | Run full recovery pipeline |
| `GET` | `/api/run-agent-stream` | ✓ | SSE: per-case pipeline traces for live view |
| `POST` | `/api/webhooks/razorpay` | sig | Real Razorpay webhook intake |

### Cases & data

| Method | Route | Description |
|---|---|---|
| `GET` | `/api/cases` | All cases with scores, sorted by recoverability |
| `GET` | `/api/cases/<id>/audit` | Full audit trail + messages + state transitions |
| `GET` | `/api/cases/<id>/explain` | SHAP feature breakdown for ML prediction |
| `GET` | `/api/cases/<id>/jobs` | Recovery jobs for a case (Phase 4) |
| `POST` | `/api/cases/<id>/replay` | ✓ Re-run a case through the pipeline |

### Analytics & metrics

| Method | Route | Description |
|---|---|---|
| `GET` | `/api/metrics` | Core KPIs + two baselines |
| `GET` | `/api/cohorts` | Recovery rates by tenure + merchant category |
| `GET` | `/api/exceptions` | Cases that ended unrecovered (JOIN query) |
| `GET` | `/api/rejected-webhooks` | Failed signature verification events |
| `GET` | `/api/activity` | Recent audit events (DESC LIMIT, not full scan) |
| `GET` | `/api/audit-check` | ✓ 7-rule correctness audit |
| `POST` | `/api/ask` | NL query → parameterized SQL + LLM summary |
| `GET` | `/api/export` | CSV summary download |
| `GET` | `/api/simulate` | ✓ Monte Carlo policy sandbox |

### Phase 5: Revenue intelligence

| Method | Route | Description |
|---|---|---|
| `GET` | `/api/intelligence/summary` | Full intelligence summary (all aggregates, one call) |
| `GET` | `/api/intelligence/by-failure-reason` | Actual recovery rates vs model priors |
| `GET` | `/api/intelligence/by-strategy` | Strategy outcomes from audit_log |
| `GET` | `/api/intelligence/incremental-revenue` | Actual vs naive vs dumb-persistence |
| `GET` | `/api/intelligence/merchant-learning` | Best strategy per merchant category |
| `GET` | `/api/risk/summary` | Top at-risk cases with scores and factors |
| `GET` | `/api/risk/case/<id>` | Risk score + contributing factors for one case |
| `GET` | `/api/anomalies` | Active anomaly alerts (6 detectors) |
| `GET` | `/api/adaptive-policy/recommend/<id>` | Data-driven strategy recommendation + explain |
| `GET` | `/api/adaptive-policy/summary` | Policy performance + governance thresholds |
| `GET` | `/api/economic-value/portfolio` | E[net_value] across all active cases |
| `GET` | `/api/economic-value/case/<id>` | EV + incremental value for one case |
| `POST` | `/api/investigate` | Revenue Investigator — analytical Q&A |

### Phase 4: Scheduler & execution

| Method | Route | Auth | Description |
|---|---|---|---|
| `POST` | `/api/scheduler/run` | ✓ | Execute all due recovery jobs |
| `GET` | `/api/scheduler/jobs` | — | List all recovery jobs |
| `GET` | `/api/scheduler/jobs/<id>` | — | Single job detail |
| `POST` | `/api/scheduler/jobs/cancel` | ✓ | Cancel a scheduled job |
| `GET` | `/api/execution/status` | — | Credential status + job summary |
| `GET` | `/api/execution/verify-credentials` | — | Live Razorpay credential probe |

---

## Data model

```
mandate_failures            (PK: customer_id)
  customer_id, amount, failure_reason, failure_date,
  past_retry_count, customer_tenure_months, past_payment_success_rate,
  merchant_category, case_status, raw_event_type,
  mandate_limit, compliance_status, dunning_stage,
  health_score, history_success_days, webhook_signature, source

audit_log                   (append-only, AUTOINCREMENT)
  event_id, customer_id → mandate_failures,
  event_timestamp, event_type, action_taken, outcome,
  attempt_number, reasoning_text, case_status_after

webhook_events              (idempotency table)
  id, razorpay_event_id UNIQUE, payload_hash,
  received_at, processed, customer_id, event_type, rejected_reason

state_transitions           (FSM history, AUTOINCREMENT)
  id, customer_id → mandate_failures,
  from_status, to_status, transitioned_at, triggered_by

recovery_jobs               (Phase 4: durable job queue)
  job_id (PK UUID), customer_id → mandate_failures,
  attempt_number, execution_mode (real_test | simulation),
  status (scheduled|claimed|executing|succeeded|failed|cancelled|exhausted),
  scheduled_at, claimed_at, executed_at, outcome,
  razorpay_payment_id, razorpay_payment_link_id, payment_link_url,
  amount_rupees, failure_reason, retry_count, max_retries,
  idempotency_key UNIQUE (customer_id:attempt_number)
```

**Indexes added (Phase 5):**
- `mandate_failures(case_status)`, `(failure_reason)`, `(merchant_category)`, `(amount)`, `(failure_date)`
- `audit_log(event_id DESC)` — for `/api/activity` DESC LIMIT query

---

## Phase 4: Real Razorpay Test Mode execution

### What works in Test Mode

| Operation | Supported | Notes |
|---|---|---|
| Webhook signature verification | ✅ Real | HMAC-SHA256 over raw bytes, `RAZORPAY_WEBHOOK_SECRET` |
| Fetch subscription status | ✅ Real | `GET /subscriptions/{id}` |
| Capture authorized payment | ✅ Real | `POST /payments/{id}/capture` — idempotent |
| Create payment link | ✅ Real | `POST /payment_links` — for customer re-authorization |
| Fetch payment details | ✅ Real | `GET /payments/{id}` |

### Honest limitation

Razorpay Test Mode **does not expose** a `POST /subscriptions/{id}/charge` endpoint. There is no API to programmatically trigger an out-of-cycle UPI debit attempt. The real execution path therefore creates a **Payment Link** for customer-driven completion. This is documented in `payment_executor.py` and shown in the dashboard.

### Execution modes

```
ExecutionMode.REAL_TEST   → calls Razorpay Test API; outcome from API response
                            Used for source='razorpay_live' cases with valid credentials

ExecutionMode.SIMULATION  → RNG-based; no HTTP calls
                            Used for synthetic benchmark/demo cases — always labelled
```

The mode is locked into the job row at scheduling time. A simulation job never silently runs as real, and a real job never falls back to simulation without an explicit `CONFIGURATION_ERROR` result.

### Running real integration tests

```bash
# Set credentials first:
export RZP_INTEGRATION=1
export RAZORPAY_KEY_ID=rzp_test_...
export RAZORPAY_KEY_SECRET=...

pytest backend/tests/test_phase4_integration.py -k "real" -v
```

---

## Phase 5: Revenue intelligence

### Risk prediction (`risk_engine.py`)

Every active case gets a **risk score (0–100)** based on:

```
urgency      = 100 - recoverability_score  (harder to recover → more urgent)
exposure     = amount / p95_amount          (high-value cases weighted up)
risk_score   = urgency × (0.6 + 0.4 × exposure) + health_modifier + over_limit_penalty
```

Each score includes **contributing factors** (recoverability score, failure reason severity, health band, retry count, over-limit status) and an **intervention window** from salary_window.py for salary-timing-sensitive cases.

Data type: `"estimate"` — derived from stored features, not a guaranteed outcome.

### Adaptive policy (`adaptive_policy.py`)

Compares the rule-based default strategy against **observed historical recovery rates** per (strategy × merchant_category). Recommends the better-performing option when sufficient data exists (≥5 cases). Every recommendation includes an `explain` trace.

**Governance tiers** (configurable via env):

| Tier | Condition | Action |
|---|---|---|
| `BLOCK` | `mandate_revoked` | Never retry — policy |
| `REQUIRE_APPROVAL` | Amount ≥ `APPROVAL_THRESHOLD_RS` (default Rs 10,000) | Explicit approval before execution |
| `RECOMMEND` | Observed rate < `LOW_CONFIDENCE_RATE` (default 50%) | Surface for review |
| `AUTO_EXECUTE` | Standard case, sufficient confidence | Proceed |

### Economic value (`economic_value.py`)

```
E[net_value] = P(recovery) × amount_recovered
              − intervention_cost  (configurable: SMS, email, retry API)
              − friction_cost      (customer LTV risk × health_band rate)
```

All cost parameters are configurable via environment variables:
`RETRY_COST_RS`, `SMS_COST_RS`, `EMAIL_COST_RS`, `FRICTION_RATE_HEALTHY/AT_RISK/HIGH_RISK`

### Anomaly detection (`anomaly_detector.py`)

Six detectors, statistical thresholds, sorted critical-first:

| Detector | Trigger condition |
|---|---|
| `failure_rate_spike` | Segment failure rate deviates > 30% from overall (relative) |
| `escalation_spike` | Escalation rate ≥ 40% overall, or +45% above average for a reason |
| `recovery_rate_drop` | Actual rate deviates > 30% from model-expected rate |
| `retry_exhaustion_pattern` | > 30% of non-revoked cases hitting the retry cap without recovery |
| `compliance_degradation` | Non-compliant pre-debit rate ≥ 25% |
| `amount_concentration` | Top 10% of cases hold > 70% of at-risk revenue |

All thresholds configurable via environment variables.

### Revenue Investigator (`/api/investigate`)

Answers analytical questions from real stored data — no hardcoded answers:

```
POST /api/investigate
{"question": "Which recovery strategy performs best?"}

→ {
    "ok": true,
    "question_type": "strategy_performance",
    "answer": "Best-performing strategy: 'higher-limit re-authorization' (100.0% recovery on 5 cases, Rs 27,236 recovered).",
    "evidence": { "by_strategy": [...] },
    "recommendation": "Prioritise 'higher-limit re-authorization' for applicable cases.",
    "data_type": "actual"
  }
```

Questions routed deterministically (no LLM needed for well-formed analytical queries):
- Recovery performance / why did recovery fall
- Which failure type has most lost revenue
- Which strategy performs best
- Revenue at risk (with [ESTIMATE] label)
- Anomalies / what is failing
- Recommendations / what should we change
- Freeform case filtering by reason/category

---

## Performance

Measured on 180 cases (warm process, development machine):

| Operation | Before Phase 5 | After Phase 5 | Notes |
|---|---|---|---|
| `/api/cases` (180 rows) | ~6,500 ms | **13 ms** | Batch ML predict + single health_score |
| `/api/exceptions` | ~14 ms (N+1) | **0.9 ms** | JOIN query |
| `/api/rejected-webhooks` | ~5 ms (N+1) | **0.2 ms** | JOIN query |
| `/api/activity` | loads all rows | **0.5 ms** | DESC LIMIT 40 |
| `risk_engine.revenue_at_risk` | new | **2.7 ms** | |
| `intelligence.full_summary` | new | **46 ms** | 6 aggregates |
| `anomaly_detector` | new | **10 ms** | 6 detectors |

The ML cold-start (pandas + sklearn import, ~3.5s) is eliminated for Flask by a background thread in `predict.py:_eager_load()` that pre-warms the model at import time.

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest                                           # 463 tests (full suite)
pytest --ignore=backend/tests/test_chaos_suite.py --ignore=backend/tests/test_benchmark.py  # fast subset
pytest backend/tests/test_performance_p5.py -s  # performance benchmarks with timings
```

### Test modules

| Module | Tests | Coverage |
|---|---|---|
| `test_scoring.py` | 18 | Formula, weights, boundaries, explain_score |
| `test_intelligence.py` | 20 | All aggregates, data_type labels, no hardcoded values |
| `test_risk_engine.py` | 12 | Score bounds, factors, severity, empty DB |
| `test_adaptive_policy.py` | 13 | Governance tiers, explain steps, batch recommend |
| `test_economic_value.py` | 15 | Formula, costs, incremental, portfolio |
| `test_anomaly_detector.py` | 15 | All 6 detectors, thresholds, data_type |
| `test_performance_p5.py` | 11 | Latency budgets, batch-vs-single, no hardcoded values |
| `test_payment_executor.py` | 24 | All outcomes, no-fake-success invariant |
| `test_scheduler.py` | 22 | Idempotency, claim, execute, stale reset |
| `test_phase4_integration.py` | 12 | Full lifecycle, duplicate guard, failure handling |
| Existing (Phases 1–3) | ~311 | Scoring, salary window, messaging, metrics, audit, chaos, concurrency, state machine, replay, security, simulation |

### Opt-in real Razorpay integration tests

```bash
RZP_INTEGRATION=1 \
RAZORPAY_KEY_ID=rzp_test_... \
RAZORPAY_KEY_SECRET=... \
pytest backend/tests/test_phase4_integration.py -k "real" -v
```

---

## Security

| Area | Implementation |
|---|---|
| Synthetic webhook signatures | HMAC-SHA256 over canonical string, `hmac.compare_digest`, fail-closed |
| Real Razorpay webhook signatures | HMAC-SHA256 over **raw body bytes** (never re-serialized), constant-time |
| Placeholder secret detection | `_INSECURE_PLACEHOLDERS` frozenset — known bad values rejected |
| API-key gate | `X-API-Key` on all mutating and compute-heavy endpoints |
| NL query injection | LLM output filtered via hardcoded field whitelist; all SQL parameterized |
| Input validation | Amount: finite, positive, ≤ Rs 1 crore; invalid events excluded from aggregates |
| Investigator trust boundary | LLM routes analytical questions; never generates SQL; fallback to deterministic routing |
| Security headers | CSP, X-Content-Type-Options, X-Frame-Options, Referrer-Policy on every response |
| Request size limit | 1 MB body limit — oversized returns 413 JSON |
| CDN integrity | Chart.js with `integrity="sha384-..."` + `crossorigin` |
| Correlation ID | Per-request `X-Correlation-ID` in all logs and responses |

---

## Data trust

Every Phase 5 metric carries an explicit `data_type` label:

| Label | Meaning |
|---|---|
| `"actual"` | Computed from real `mandate_failures` / `audit_log` rows |
| `"estimate"` | Derived from the probability model or configurable parameters |
| `"simulation"` | Monte Carlo RNG (Policy Sandbox, strategy comparison) |
| `"mixed"` | Response contains both actual and estimated fields — each field is individually labelled |

No metric is hardcoded. All baselines are clearly marked `[ESTIMATE — simulation-based counterfactual]`.

---

## ML validation layer

```bash
python backend/ml/train_model.py   # trains LR vs GBM, writes model.pkl + metrics.json
```

- Competes LogisticRegression vs GradientBoostingClassifier on stratified 80/20 split
- Winner saved by ROC-AUC
- SHAP values per case (case detail drawer) + global importance (ML Insights tab)
- **Non-decision**: the model validates the rule-based scoring; it never drives retry/escalation/compliance
- **Synthetic training data**: labels come from simulation runs, not real payments — explicitly stated throughout the UI

---

## Architecture decisions

**1. Four-agent pipeline with append-only audit trail.**
Each agent has one responsibility. Every decision is a row in `audit_log`. The trail is immutable — no UPDATE or DELETE on audit rows — so compliance checks and case replay always see the original decision.

**2. Idempotency at the DB layer, not application code.**
`webhook_events.razorpay_event_id UNIQUE` and `recovery_jobs.idempotency_key UNIQUE` prevent double processing even if the application crashes between check and write. `BEGIN IMMEDIATE` serialises concurrent workers before the terminal-audit check.

**3. Explicit FSM with recorded transitions.**
`LEGAL_TRANSITIONS` in `db.py` defines every valid `case_status` change. `_RunContext.set_status()` validates at runtime; an illegal transition raises `ValueError` immediately.

**4. LLM for narration only — never decisions.**
The LLM narrates already-made rule-based decisions. It never drives retry/escalation/compliance. If it's unavailable, template fallbacks maintain full functionality.

**5. Explicit execution modes — no silent fallback.**
`ExecutionMode.REAL_TEST` calls Razorpay. `ExecutionMode.SIMULATION` uses RNG. If real execution is requested but credentials are absent, the job fails with `CONFIGURATION_ERROR` — not silently marked as recovered.

**6. data_type labels on all intelligence outputs.**
Every Phase 5 function attaches `data_type: "actual" | "estimate" | "simulation" | "mixed"` so the UI always renders the correct label. A simulated outcome never silently appears as a real recovery.

**7. Performance measured before optimizing.**
The 6,500ms `/api/cases` bottleneck was measured (N×DataFrame construction + ML predict). The fix (batch prediction + single health_score) was verified to reduce it to 13ms. No optimization was added without measurement.

---

## Known limitations

| Feature | Status |
|---|---|
| UPI debit trigger via API | **Not possible in Razorpay Test Mode** — no `POST /subscriptions/{id}/charge` endpoint exists |
| Message delivery | **Generated, not sent** — templates only |
| LLM model | `openai/gpt-oss-20b` via Groq — falls back to templates on rate-limit; `LLM_LIVE_TOP_N=5` by default to respect 8K TPM free-tier |
| ML training data | **Synthetic** — labels from simulation, not real payments |
| Multi-worker | SQLite single-writer; `busy_timeout=15s` + `BEGIN IMMEDIATE` handles brief contention |
| Authentication | Single shared API key — no user accounts or token rotation |
| Rate limiting | Not implemented on public read endpoints |

---

## Project structure

```
Mandate_Rescue/
├── backend/
│   ├── app.py                # Flask app, 49 routes, SSE, security middleware
│   ├── agent.py              # Four-agent pipeline + execution mode dispatch
│   ├── db.py                 # SQLite DAL, FSM, recovery_jobs queue
│   ├── seed.py               # 180-record synthetic generator (seed=42)
│   ├── scoring.py            # Recoverability score (weighted 0-100)
│   ├── salary_window.py      # Salary-window inference
│   ├── messaging.py          # Template nudges (Standard + Hinglish)
│   ├── llm_client.py         # LLM wrapper with fast-fail on rate-limit
│   ├── query.py              # NL query → parameterized SQL
│   ├── metrics.py            # KPI aggregations (JOIN queries, no N+1)
│   ├── baseline.py           # Naive + dumb-persistence baselines
│   ├── health.py             # Subscription health score
│   ├── security.py           # API-key gate
│   ├── webhook_security.py   # Fail-closed HMAC for synthetic pipeline
│   ├── razorpay_adapter.py   # Real Razorpay webhook verification + mapping
│   ├── razorpay_client.py    # Test-mode API client (plans, subscriptions, payments, links)
│   ├── payment_executor.py   # Phase 4: REAL_TEST / SIMULATION execution service
│   ├── scheduler.py          # Phase 4: durable job queue worker
│   ├── intelligence.py       # Phase 5: strategy/failure-reason/merchant analytics
│   ├── risk_engine.py        # Phase 5: revenue-at-risk prediction
│   ├── adaptive_policy.py    # Phase 5: data-driven strategy recommendation + governance
│   ├── economic_value.py     # Phase 5: E[net_value] per intervention
│   ├── anomaly_detector.py   # Phase 5: 6 statistical anomaly detectors
│   ├── simulation_runner.py  # Monte Carlo policy sandbox
│   ├── export.py             # CSV export
│   ├── audit_check.py        # 7-rule correctness audit (read-only)
│   ├── chaos_test.py         # 10 adversarial scenarios (isolated in-memory DBs)
│   ├── ml/
│   │   ├── train_model.py    # LR vs GBM, stratified split, model.pkl + metrics.json
│   │   ├── predict.py        # Batch-optimised inference + background warm-up
│   │   └── explain.py        # SHAP per-case + global importance
│   └── tests/                # 463 tests across 30 test files
│       ├── test_intelligence.py
│       ├── test_risk_engine.py
│       ├── test_adaptive_policy.py
│       ├── test_economic_value.py
│       ├── test_anomaly_detector.py
│       ├── test_performance_p5.py
│       ├── test_payment_executor.py
│       ├── test_scheduler.py
│       ├── test_phase4_integration.py
│       └── ... (21 more test files)
├── benchmark.py              # Reproducible 3-strategy comparison CLI
├── frontend/
│   ├── templates/index.html  # SPA: 9 views including Analytics (Phase 5)
│   └── static/
│       ├── style.css         # Design system: Razorpay-inspired, light/dark
│       └── app.js            # Dashboard logic, Phase 4/5 panels
├── .github/workflows/ci.yml  # GitHub Actions: pytest + audit + chaos
├── Dockerfile                # Gunicorn, non-root, tini, healthcheck
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt
└── pytest.ini
```

---

## Benchmark

```bash
python benchmark.py --n-runs 30 --seed 42
```

Compares Baseline A (naive, 1 attempt), Baseline B (dumb persistence, 3 attempts), and Mandate Rescue (full pipeline). All use the same probability model — only strategy differs. Results are Monte Carlo means ± 95% CI (Student's t).

---

## Phase 6 — Closed-Loop Adaptive Revenue Optimization

Phase 6 turns Mandate Rescue from an intelligent recovery engine into an adaptive revenue-optimization system. The learning loop is:

`
PREDICT → DECIDE → ACT → OBSERVE OUTCOME → MEASURE → COMPARE
    → LEARN → RECOMMEND → MERCHANT APPROVES → POLICY ACTIVATES → REPEAT
`

### Closed-Loop Architecture

`
Recovery decision
      │
      ▼
 Execution (real_test / simulation)
      │
      ▼
 Outcome observed
      │
      ▼
 outcome_attribution.py   ← writes strategy_performance (3 dimensions, provenance-tagged)
      │
      ▼
 segment_learning.py      ← fallback hierarchy: merchant → failure_reason → global → rule_based
      │
      ▼
 policy_engine.py         ← generate_recommendations (evidence-gated, deduplicated)
      │
      ▼
 Merchant reviews
      │
      ▼
 approve_recommendation() ← creates + activates policy_version
      │
      ▼
 Active policy            ← governs future recovery decisions
      │
      ▼
 record_current_policy_performance() ← measures before/after
      │
      └──────────────────────────────────────────────────────► REPEAT
`

### New Phase 6 Database Tables

| Table | Purpose |
|---|---|
| strategy_performance | Durable per-dimension strategy stats with provenance |
| experiments | Controlled A/B experiment definitions |
| experiment_assignments | Case-to-arm mapping (deterministic hash, immutable) |
| experiment_outcomes | Final outcome per case per experiment (write-once) |
| policy_versions | Immutable version history (DRAFT→RECOMMENDED→APPROVED→ACTIVE→DEPRECATED) |
| policy_performance | Measured recovery rate per version after activation |
| policy_recommendations | Data-backed recommendations with evidence trail |
| policy_audit_log | Append-only governance action record |

### Strategy Evaluation Methodology

Performance is tracked per dimension: global, ailure_reason, merchant_category.
Each dimension × strategy × provenance is a separate row — REAL_TEST is never silently combined with SIMULATION.

**Fallback hierarchy** (segment_learning.py):
1. Merchant-specific (≥ 10 observations required)
2. Failure-reason-specific
3. Global
4. Rule-based default

A strategy change is only recommended when it outperforms the default by ≥ 3pp AND has ≥ 10 observations.

### Experimentation Methodology

- **Arm assignment**: deterministic SHA-256 hash — same case always lands in same arm
- **Outcome recording**: UNIQUE constraint prevents duplicate counts
- **Minimum sample**: configurable (default 10 per arm). Below threshold returns sufficient_data: false — never manufactures a result
- **Statistical test**: two-proportion z-test at ≥ 30 observations; confidence: high (z ≥ 2.576), moderate (z ≥ 1.96), low (z ≥ 1.282)
- **Incremental revenue**: rate_diff × treatment_amount_attempted. Always labelled [ESTIMATE]. Total recovered ≠ incremental

### Counterfactual Methodology

Every counterfactual output:
1. States the observed outcome separately from the estimated counterfactual
2. Labels every estimate [ESTIMATE — counterfactual, not causal proof]
3. Never calls simulated results "real-world performance"

### Evidence / Data Provenance

| Tag | Meaning |
|---|---|
| REAL_TEST | source=azorpay_live AND execution_mode=eal_test |
| SIMULATION | execution_mode=simulation |
| HISTORICAL | Pre-Phase-4 pipeline run (no recovery jobs) |
| ESTIMATE | Probability-model output (no execution observed) |

The Learning dashboard shows a provenance breakdown and warns prominently when no REAL_TEST outcomes exist.

### Policy Governance

Policy version lifecycle: DRAFT → RECOMMENDED → UNDER_REVIEW → APPROVED → ACTIVE → DEPRECATED / ROLLED_BACK

- Versions are **immutable** after creation — only status transitions allowed
- New version only created on **explicit named approval**
- Activating a version **auto-deprecates** the previous active one
- Rollback **preserves** all historical performance records — history is never rewritten
- Every governance action recorded in policy_audit_log with actor, timestamp, and status change

**Insufficient-data protection:** generate_recommendations() only fires when sample ≥ 10 AND improvement ≥ 3pp. Returns [] otherwise — never manufactures confidence.

**mandate_revoked safety gate:** est_strategy_for_case() always returns "immediate escalation" for mandate_revoked, regardless of observed data. This rule cannot be overridden by the learning system.

### Strategy Drift Detection

Compares performance in the recent window (default: last 30 days) vs all prior data. A drift alert is raised when the relative drop exceeds 15%. Both windows must have ≥ 5 cases; under-sampled strategies are surfaced separately, never silently ignored.

### Limitations

1. **No causal proof.** Experiment results are correlational within the experiment design. Causal claims require a properly powered randomized controlled trial.
2. **Synthetic data dominates until real runs occur.** All 180 seed cases are HISTORICAL/SIMULATION. No REAL_TEST data exists until real Razorpay Test Mode webhooks are received and executed.
3. **Small samples.** 180 cases across 4 × 4 segments = ~11 cases per cell. Most cells stay below MIN_SAMPLE and produce no recommendations — this is correct behaviour, not a bug.
4. **No time-series aggregation.** strategy_performance accumulates totals. Rolling windows in strategy_drift use ailure_date as a proxy for execution date.
5. **Single-tenant.** Policy engine uses merchant_category = "all" as the global namespace. Per-merchant scoping requires a merchant_id column throughout.

---

## Phase 6 API Reference

All Phase 6 endpoints: /api/learning/*. Mutating endpoints require X-API-Key.

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| GET | /api/learning/attribution/summary | — | Attribution coverage and provenance breakdown |
| POST | /api/learning/backfill | ✓ | Backfill strategy_performance from audit log (idempotent) |
| GET | /api/learning/strategy-performance | — | All strategy performance records |
| GET | /api/learning/segment-learning | — | Full learning summary across all dimensions |
| GET | /api/learning/strategy-drift | — | Strategy drift alerts |
| GET | /api/learning/experiments | — | List experiments |
| POST | /api/learning/experiments/create | ✓ | Create A/B experiment |
| GET | /api/learning/experiments/<id> | — | Experiment status + evaluation |
| POST | /api/learning/experiments/complete | ✓ | Mark experiment completed |
| GET | /api/learning/recommendations | — | List recommendations |
| POST | /api/learning/recommendations/generate | ✓ | Generate recommendations from evidence |
| GET | /api/learning/recommendations/<id> | — | Full recommendation + evidence trail |
| POST | /api/learning/recommendations/approve | ✓ | Approve → creates + activates policy version |
| POST | /api/learning/recommendations/reject | ✓ | Reject with reason |
| GET | /api/learning/policy/active | — | Currently active policy |
| GET | /api/learning/policy/history | — | Full version history |
| GET | /api/learning/policy/<version_id> | — | Version detail + performance + audit |
| POST | /api/learning/policy/measure | ✓ | Record current performance snapshot |
| POST | /api/learning/policy/rollback | ✓ | Roll back to a previous version |
| GET | /api/learning/policy/audit-log | — | Full governance audit log |
| GET | /api/learning/dashboard | — | Single-call Learning view payload |

---


---

## Phase 6 — Closed-Loop Adaptive Revenue Optimization

Phase 6 turns Mandate Rescue from an intelligent recovery engine into an adaptive revenue-optimization system. The learning loop is:

```
PREDICT → DECIDE → ACT → OBSERVE OUTCOME → MEASURE → COMPARE
    → LEARN → RECOMMEND → MERCHANT APPROVES → POLICY ACTIVATES → REPEAT
```

### Closed-Loop Architecture

```
Recovery decision
      │
      ▼
 Execution (real_test / simulation)
      │
      ▼
 Outcome observed
      │
      ▼
 outcome_attribution.py   ← writes strategy_performance (3 dimensions, provenance-tagged)
      │
      ▼
 segment_learning.py      ← fallback hierarchy: merchant → failure_reason → global → rule_based
      │
      ▼
 policy_engine.py         ← generate_recommendations (evidence-gated, deduplicated)
      │
      ▼
 Merchant reviews
      │
      ▼
 approve_recommendation() ← creates + activates policy_version
      │
      ▼
 Active policy            ← governs future recovery decisions
      │
      ▼
 record_current_policy_performance() ← measures before/after
      │
      └─────────────────────────────────────────────────────────► REPEAT
```

### New Phase 6 Database Tables

| Table | Purpose |
|---|---|
| `strategy_performance` | Durable per-dimension strategy stats with provenance tags |
| `experiments` | Controlled A/B experiment definitions |
| `experiment_assignments` | Case-to-arm mapping (deterministic hash, immutable) |
| `experiment_outcomes` | Final outcome per case per experiment (write-once) |
| `policy_versions` | Immutable version history: DRAFT → RECOMMENDED → UNDER_REVIEW → APPROVED → ACTIVE → DEPRECATED/ROLLED_BACK |
| `policy_performance` | Measured recovery rate per version after activation |
| `policy_recommendations` | Data-backed recommendations with full evidence trail |
| `policy_audit_log` | Append-only governance action record |

### Strategy Evaluation Methodology

Performance is tracked per dimension: `global`, `failure_reason`, `merchant_category`.
Each (dimension × strategy × provenance) combination is a separate row — `REAL_TEST` is never silently combined with `SIMULATION`.

**Fallback hierarchy** (`segment_learning.py`):
1. Merchant-specific (≥ 10 observations required)
2. Failure-reason-specific
3. Global
4. Rule-based default (no data at all)

A strategy change is only recommended when the alternative outperforms the default by ≥ 3 percentage points **and** has ≥ 10 observations.

### Experimentation Methodology

- **Arm assignment**: deterministic SHA-256 hash of `(experiment_id, customer_id)` — same case always lands in the same arm regardless of how many times assignment runs
- **Outcome recording**: UNIQUE constraint on `(experiment_id, customer_id)` prevents duplicate counts
- **Minimum sample**: configurable (default 10 per arm). Below threshold `evaluate_experiment()` returns `sufficient_data: false` — never manufactures a result
- **Statistical test**: two-proportion z-test at ≥ 30 observations per arm; confidence: `high` (z ≥ 2.576), `moderate` (z ≥ 1.96), `low` (z ≥ 1.282), `very_low` otherwise
- **Incremental revenue**: estimated as `rate_diff × treatment_amount_attempted`. Always labelled `data_type: "estimate"`. Total recovered revenue ≠ incremental revenue

### Counterfactual Methodology

Every counterfactual output:
1. States the **observed** outcome separately from the **estimated** counterfactual
2. Labels every estimate `[ESTIMATE — counterfactual, not causal proof]`
3. Uses only observed rate difference × observed amount as the incremental estimate
4. Never calls simulated results "real-world performance"

### Evidence / Data Provenance

Every strategy performance record carries a `provenance` tag:

| Tag | Meaning |
|---|---|
| `REAL_TEST` | `case.source = razorpay_live` AND `execution_mode = real_test` |
| `SIMULATION` | `execution_mode = simulation` |
| `HISTORICAL` | Pre-Phase-4 agent pipeline run (no recovery jobs existed yet) |
| `ESTIMATE` | Probability-model output — no execution observed |

The Learning dashboard shows a provenance breakdown and warns prominently when no REAL_TEST observations exist.

### Policy Governance

Version lifecycle:
```
DRAFT → RECOMMENDED → UNDER_REVIEW → APPROVED → ACTIVE → DEPRECATED / ROLLED_BACK
```

Rules enforced:
- Versions are **immutable** after creation — only status transitions are allowed
- New versions are only created on **explicit named approval** of a recommendation
- Activating a new version **automatically deprecates** the previous active one for the same merchant category
- Rollback **preserves** all historical performance records — history is never rewritten
- Every governance action is recorded in `policy_audit_log` with actor, timestamp, previous status, and new status

**Insufficient-data protection:** `generate_recommendations()` only fires when sample ≥ 10 **and** improvement ≥ 3pp **and** confidence ≥ `low`. Returns `[]` otherwise — never manufactures confidence.

**mandate_revoked safety gate:** `best_strategy_for_case()` always returns `"immediate escalation"` for `mandate_revoked` cases, regardless of any observed data. This compliance rule cannot be overridden by the learning system.

### Strategy Drift Detection

`strategy_drift.py` compares performance in the **recent window** (default: last 30 days) vs all prior data. An alert is raised when the relative performance drop exceeds 15%.

Both windows must have ≥ 5 cases. Under-sampled strategies are surfaced explicitly in `insufficient_data_strategies` — never silently ignored.

Possible causes investigated per alert: failure-type distribution shift, payment-method mix change, merchant-side policy change, retry timing drift, seasonal behaviour.

### Limitations

1. **No causal proof.** Experiment results show correlation within the experiment design. Only a properly powered randomized controlled trial provides causal evidence.
2. **Synthetic data dominates until real runs.** All 180 seed cases are `HISTORICAL` or `SIMULATION`. No `REAL_TEST` data exists until real Razorpay Test Mode webhooks are received and executed. The dashboard makes this explicit.
3. **Small samples per segment.** 180 cases across 4 failure reasons × 4 merchant categories ≈ 11 cases per cell. Most cells stay below `MIN_SAMPLE` and produce no recommendations. This is intentional and correct — not a bug.
4. **No time-series aggregation.** `strategy_performance` accumulates totals without per-day granularity. Rolling-window drift uses `failure_date` as an approximation for when cases were processed.
5. **Single-tenant.** The policy engine operates on `merchant_category = "all"` as the global namespace. Per-merchant scoping requires a `merchant_id` column throughout the schema.

---

## Phase 6 API Reference

All Phase 6 endpoints are prefixed `/api/learning/`. Mutating endpoints require `X-API-Key`.

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| GET | `/api/learning/attribution/summary` | — | Attribution coverage and provenance breakdown |
| POST | `/api/learning/backfill` | ✓ | Backfill strategy_performance from audit log (idempotent) |
| GET | `/api/learning/strategy-performance` | — | All strategy performance records (filterable) |
| GET | `/api/learning/segment-learning` | — | Full learning summary across all dimensions |
| GET | `/api/learning/strategy-drift` | — | Strategy drift detection alerts |
| GET | `/api/learning/experiments` | — | List all experiments |
| POST | `/api/learning/experiments/create` | ✓ | Create a new A/B experiment |
| GET | `/api/learning/experiments/<id>` | — | Experiment status + evaluation results |
| POST | `/api/learning/experiments/complete` | ✓ | Mark experiment completed, record outcomes |
| GET | `/api/learning/recommendations` | — | List recommendations (filterable by status) |
| POST | `/api/learning/recommendations/generate` | ✓ | Generate new recommendations from evidence |
| GET | `/api/learning/recommendations/<id>` | — | Full recommendation detail + evidence trail |
| POST | `/api/learning/recommendations/approve` | ✓ | Approve → creates and activates policy version |
| POST | `/api/learning/recommendations/reject` | ✓ | Reject with required reason |
| GET | `/api/learning/policy/active` | — | Currently active policy (or rule-based defaults) |
| GET | `/api/learning/policy/history` | — | Full version history for a merchant category |
| GET | `/api/learning/policy/<version_id>` | — | Version detail + performance records + audit trail |
| POST | `/api/learning/policy/measure` | ✓ | Record current performance snapshot for active policy |
| POST | `/api/learning/policy/rollback` | ✓ | Roll back to a named previous version |
| GET | `/api/learning/policy/audit-log` | — | Full governance audit log |
| GET | `/api/learning/dashboard` | — | Single-call payload for the Learning view |


## Roadmap

- Replace SQLite with PostgreSQL for multi-worker production deployment
- Real UPI debit trigger when Razorpay exposes a suitable API
- SMS/WhatsApp delivery via Razorpay Messages
- Per-user authentication with merchant scoping
- Real ML training data from anonymised historical outcomes
- Time-series dashboards (daily/weekly trend charts)
- Reinforcement learning feedback loop for adaptive policy weights
