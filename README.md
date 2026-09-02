# Mandate Rescue

[![CI](https://github.com/OWNER/Mandate_Rescue/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/Mandate_Rescue/actions/workflows/ci.yml)

> **An event-driven payment recovery platform** that detects failed recurring payments, diagnoses the failure, decides the optimal recovery strategy, executes recovery actions, tracks outcomes, and provides merchants with explainable recovery intelligence.

---

## Problem

In India, recurring payments via UPI Autopay and NACH e-mandates fail at significant rates due to insufficient funds, expired mandates, revoked authorisations, and bank-side technical errors. Every failure silently erodes subscription revenue. Most platforms retry blindly — the same attempt at the same time, for every failure, regardless of customer history or failure type. That wastes retries, irritates customers, and misses recoverable revenue.

## Solution

Mandate Rescue replaces the "retry everyone the same way" approach with an intelligent, explainable recovery pipeline:

- **Classify** each failure into a specific reason (insufficient funds, mandate expired, revoked, bank error)
- **Score** each case on recoverability using customer history and failure type
- **Triage** highest-value cases first
- **Choose** the optimal strategy per case: salary-window retry, re-authorisation link, dunning escalation, or immediate escalation (for revoked mandates)
- **Generate** customer-facing nudges (Standard + Hinglish) for the right channel at the right stage
- **Track** every decision in an append-only audit trail, re-verified by an independent correctness audit
- **Explain** every recovery decision with scores, factors, and reasoning

---

## Architecture

```
Razorpay Webhook (real / test-mode)   OR   Synthetic 180-case seed
          │                                          │
          ▼                                          ▼
 ┌─────────────────────────────────────────────────────────────┐
 │                    Webhook Gateway                          │
 │  • Signature verification (HMAC-SHA256, constant-time)      │
 │  • Idempotency check (webhook_events table, UNIQUE)         │
 │  • Payload validation (amount > 0, finite, ≤ Rs 1 crore)   │
 │  • Event persistence → mandate_failures + audit_log         │
 └──────────────────────────┬──────────────────────────────────┘
                            │
                            ▼
 ┌─────────────────────────────────────────────────────────────┐
 │                 Recovery Orchestrator                       │
 │                                                             │
 │  DiagnosisAgent  →  TriageAgent  →  StrategyAgent           │
 │       │                │                  │                 │
 │  Classify failure   Score (0-100)    Per-reason strategy    │
 │  Map event type     Health score     Retry cap gate         │
 │  Verify signature   Triage order     Mandate-limit gate     │
 │                                      RBI pre-debit rule     │
 │                                      Dunning sequence       │
 │                                           │                 │
 │                                    CommunicationAgent       │
 │                                    LLM narration / msgs     │
 └──────────────────────────┬──────────────────────────────────┘
                            │
               ┌────────────┼────────────┐
               ▼            ▼            ▼
          Recovered     Escalated    Rejected
          (audit log)  (audit log)  (signature fail)
               │
               ▼
 ┌─────────────────────────────────────────────────────────────┐
 │  Analytics / Audit / Dashboard                              │
 │  • Recovery funnel, KPIs, cohort breakdown                  │
 │  • Agent vs two baselines (naive + dumb persistence)        │
 │  • Correctness audit (7 re-derived business rules)          │
 │  • ML validation layer (scikit-learn + SHAP)                │
 │  • Case Replay, Webhook Inspector, Policy Sandbox           │
 │  • "Ask the data" (NL → parameterized SQL)                  │
 └─────────────────────────────────────────────────────────────┘
```

### Event flow

```
12:01:03  Webhook received (POST /api/webhooks/razorpay or synthetic seed)
12:01:03  Signature verified (HMAC-SHA256 over raw body, constant-time)
12:01:03  Idempotency checked (webhook_events.razorpay_event_id UNIQUE)
12:01:03  Event persisted (mandate_failures + webhook_events)
12:01:04  DiagnosisAgent: failure_reason + raw_event_type classified
12:01:04  TriageAgent: recoverability score computed, triage order set
12:01:04  StrategyAgent: retry scheduled (salary-window day 1-3), pre-debit notification logged
16:30:00  StrategyAgent: retry executed → recovered
16:30:01  Outcome persisted (audit_log: case_status = recovered)
16:30:01  Dashboard KPIs updated (real aggregate from audit rows)
```

---

## AI / Agent architecture

| Agent | Input | Decision | Output | Fallback |
|---|---|---|---|---|
| **DiagnosisAgent** | Raw webhook payload + signature | Classify failure reason, verify signature | `failure_reason`, `raw_event_type`, or reject | Reject → `webhook_rejected` audit row |
| **TriageAgent** | Case fields, past success rate, tenure, retry count | Weighted 0-100 recoverability score | Score, health band, triage ordering | Score=0, escalate |
| **StrategyAgent** | Failure reason, score, mandate limit, RBI clock | Select retry strategy, enforce compliance | Retry / dunning / reauth / escalation + audit row per step | Hard cap at 3 retries, escalate |
| **CommunicationAgent** | Case + triage + decisions | Generate readable reasoning + customer nudge | LLM text or template fallback | Template always available |

**Trust boundary:** The LLM narrates decisions and translates query intent — it never makes a decision, never sees secrets, and never generates SQL directly. The `/api/ask` endpoint enforces a hardcoded field whitelist before any filter reaches the database.

---

## Data model

```
mandate_failures (PRIMARY KEY: customer_id)
  customer_id, amount, failure_reason, failure_date,
  past_retry_count, customer_tenure_months, past_payment_success_rate,
  merchant_category, case_status, raw_event_type,
  mandate_limit, compliance_status, dunning_stage,
  health_score, history_success_days, webhook_signature, source

audit_log (append-only, AUTOINCREMENT)
  event_id, customer_id → mandate_failures,
  event_timestamp, event_type, action_taken, outcome,
  attempt_number, reasoning_text, case_status_after

webhook_events (idempotency table)
  id, razorpay_event_id UNIQUE, payload_hash,
  received_at, processed, customer_id, event_type, rejected_reason

state_transitions (state machine history, AUTOINCREMENT)
  id, customer_id → mandate_failures,
  from_status, to_status, transitioned_at, triggered_by
```

Key design decisions:
- `audit_log` is append-only — no UPDATE or per-row DELETE exists anywhere in the code
- `webhook_events.razorpay_event_id` has a UNIQUE constraint → duplicate delivery is safe at the DB level, not just in application code
- `state_transitions` records every legal `case_status` change — illegal transitions (e.g. `recovered → in_progress`) are rejected at the application layer with a `ValueError`, not just ignored
- `LEGAL_TRANSITIONS` map in `db.py` defines the explicit state machine: `new → in_progress → recovered|escalated|promised|broken_promise`; terminal states have no outbound transitions
- `PRAGMA foreign_keys = ON` — FK from `audit_log` to `mandate_failures` is enforced
- `busy_timeout = 15000` — brief write contention between SSE stream and concurrent reads is handled safely
- `invalid` and `duplicate` case statuses are excluded from all money aggregates
- **Concurrent recovery protection**: `_acquire_processing_lock` issues `BEGIN IMMEDIATE` before the idempotency check, so two concurrent workers cannot both pass "not terminal" and double-process the same case

---

## Security

| Area | Implementation |
|---|---|
| Synthetic webhook signatures | HMAC-SHA256 over canonical string, `hmac.compare_digest`, fail-closed — no default secret |
| Real Razorpay webhook signatures | HMAC-SHA256 over **raw request body bytes** (never re-serialized), constant-time compare, fail-closed |
| Placeholder secret detection | `_INSECURE_PLACEHOLDERS` frozenset — known bad values rejected at startup |
| API-key gate | `X-API-Key` required on all mutating endpoints; constant-time compare; auto-generated if unset |
| NL query injection | LLM output filtered against a hardcoded field whitelist; all SQL is parameterized |
| Input validation | Amount validated: finite, positive, ≤ Rs 1 crore; invalid events logged + excluded from all aggregates |
| Question length limit | `/api/ask` rejects questions > 500 chars to prevent LLM prompt stuffing |
| Security headers | CSP, X-Content-Type-Options, X-Frame-Options, Referrer-Policy on every response |
| Correlation ID | `X-Correlation-ID` injected per-request into all log records and echoed in responses |
| Request size limit | `MAX_CONTENT_LENGTH = 1 MB` — oversized bodies return 413 JSON |
| CDN integrity | Chart.js loaded with `integrity="sha384-..."` + `crossorigin="anonymous"` |
| Sensitive endpoints | `/api/audit-check` and `/api/chaos-test` are API-key gated (compute-heavy) |
| Webhook rollback | Persistence errors in `/api/webhooks/razorpay` roll back atomically — no partial state |

**Known limitations (appropriate for this scope):** single shared API key, no user accounts, no token rotation, no rate limiting on public read endpoints.

---

## Razorpay integration

`POST /api/webhooks/razorpay` is a real Razorpay webhook receiver:

1. **Verification** — `razorpay_adapter.verify_razorpay_signature()` computes HMAC-SHA256 over the **raw request body bytes** and compares it in constant time against `X-Razorpay-Signature`, keyed with `RAZORPAY_WEBHOOK_SECRET` (configured in the Razorpay Dashboard → Settings → Webhooks).
2. **Idempotency** — `claim_webhook_event()` inserts into `webhook_events` with a UNIQUE constraint on Razorpay's own event `id`. A duplicate delivery returns `{status: "already_processed"}` without re-processing.
3. **Mapping** — `map_razorpay_event()` extracts `customer_id` from `notes`, falls back to Razorpay's own subscription/payment ID, converts paise to rupees, and creates a case record in the same shape as the synthetic seed.
4. **Identical pipeline** — the mapped record flows through `DiagnosisAgent → TriageAgent → StrategyAgent → CommunicationAgent` identically to synthetic cases, tagged `source: "razorpay_live"`.

**Supported real Razorpay events:**

| Razorpay event | Internal reason |
|---|---|
| `subscription.charged.failed`, `payment.failed` | `insufficient_funds` |
| `subscription.halted`, `subscription.cancelled` | `mandate_revoked` |
| `subscription.pending` | `mandate_expired` |
| `payment.dispute.created` | `bank_technical_error` |

**Test-mode setup:** `razorpay_client.py` creates a test-mode plan + subscription via `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET`. Use [ngrok](https://ngrok.com) or [smee.io](https://smee.io) to expose `/api/webhooks/razorpay` for local Razorpay test delivery.

**Honest distinction:** This project uses Razorpay **test mode** for webhook intake. No real money changes hands. The 180-case synthetic simulation is what runs for scale and demo purposes. A real Razorpay event arriving via the webhook endpoint is a genuine integration, not a simulation.

---

## Recovery metrics — how they're calculated

| Metric | Formula | Source |
|---|---|---|
| Amount at risk | Sum of `amount` for all cases excluding `invalid` + `duplicate` | `mandate_failures` |
| Amount recovered | Sum of `amount` where `case_status = recovered` | `mandate_failures` |
| Recovery rate | `recovered_cases / total_cases` (excl. invalid/duplicate) | `mandate_failures` |
| Escalation rate | `escalated_cases / total_cases` | `mandate_failures` |
| Naive baseline | 1 attempt per case, same probability model, RNG seed 42 | `baseline.run_baseline()` |
| Dumb persistence | Up to 3 attempts per case, no strategy, RNG seed 43 | `baseline.run_dumb_persistence_baseline()` |

**Why two baselines:** The naive baseline answers "did the agent beat doing nothing?". The dumb-persistence baseline answers "did the agent's *strategy* (scoring, timing, dunning) add value beyond just retrying more?". The second is the harder, more defensible claim.

**Simulation label:** All recovery outcomes are stochastic simulations — `rng.random() < recovery_probability` — not real payment API calls. Probability is blended from `BASE_SUCCESS_PROB[failure_reason]` and the 0-100 recoverability score. This is clearly labeled as simulation throughout the UI.

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest -v                          # full suite (~321 tests, excludes slow)
pytest -m "not slow" -v            # fast tests only (~317 tests)
pytest -m slow -v                  # volume tests only (~4 tests)
pytest --cov=backend --cov-report=term-missing   # with coverage
```

### Coverage

| Module | Tests | What's covered |
|---|---|---|
| `scoring.py` | 21 | Formula, weight normalization, REASON_BASE, boundaries, explain_score |
| `salary_window.py` | 21 | Both modes, history parsing, modal inference, window clamping |
| `health.py` | 22 | Score formula, band thresholds, boundaries, health_for_case |
| `messaging.py` | 23 | All 4 templates × 4 categories, Hinglish, channel validation, masking |
| `query.py` | 26 | All filter types, whitelist enforcement, computed filters, sort, limit |
| `metrics.py` | 22 | Core KPIs, invalid/duplicate exclusions, cohorts, exceptions, rejected |
| `metric_correctness` | 17 | Formula verification, no double-counting, baseline purity, cohort sums |
| `export.py` | 15 | CSV structure, all sections, real metric values, baseline rows |
| `simulation_runner.py` | 33 | CI math, t vs normal, paired delta, run shape, compare_policies |
| `webhook_security.py` | — | HMAC round-trip, missing/wrong/placeholder secrets |
| `razorpay_adapter.py` | — | Signature round-trip, body tampering, event mapping, fallbacks |
| `/api/ask` (Flask) | 16 | LLM stub paths, whitelist, injection blocking, error codes |
| Audit invariants | 9 | All 7 business rules + reproducible counts + run_completed |
| Idempotency | — | Duplicate webhook, re-run agent, pinned 139/38/3 counts |
| State machine | 20 | Legal/illegal transitions, DB enforcement, full-run consistency |
| Concurrency | 5 | Sequential double-process, concurrent pipeline, UNIQUE constraint race |
| Chaos suite | 10 | Replay, invalid amounts, dup IDs, clock skew, malformed LLM, sig edge, extreme volume, malformed body, restart safety, retry exhaustion |
| Replay endpoint | 6 | Auth, 404, new case processing, terminal idempotency |
| Benchmark | 15 | Structure, reproducibility, CI math helpers |
| Security | — | API key gate, Razorpay webhook route end-to-end |
| Baselines | — | Shape, side-effects, dumb ≥ naive |

Also verified by standalone CLI tools:

```bash
python backend/audit_check.py          # 7 business-rule correctness checks
python backend/chaos_test.py           # 10 adversarial scenarios (isolated in-memory DBs)
python benchmark.py --n-runs 30        # Reproducible 3-strategy comparison
```

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
WEBHOOK_SECRET=<generate: python -c "import secrets; print(secrets.token_hex(32))">

# Required for real Razorpay webhook intake (optional if only using synthetic demo)
RAZORPAY_WEBHOOK_SECRET=<from Razorpay Dashboard → Settings → Webhooks>

# Optional — LLM narration (falls back to templates without this)
GROQ_API_KEY=gsk_...

# Optional — auto-generated and printed to log on startup if omitted
MANDATE_RESCUE_API_KEY=<generate: python -c "import secrets; print(secrets.token_urlsafe(32))">
```

### 3. Run

```bash
python backend/app.py
```

Open http://127.0.0.1:5000. Click **Reset demo** to seed 180 cases, then **Run agent** to watch the pipeline live.

For a persistent key (useful for scripting):

```bash
# PowerShell
$env:MANDATE_RESCUE_API_KEY = "my-fixed-key"
python backend/app.py
```

---

## Docker

```bash
cp .env.example .env   # fill in WEBHOOK_SECRET at minimum
touch mandate_rescue.db    # create DB file (not directory) before bind-mount
docker compose up --build
```

Opens on http://127.0.0.1:5000. The SQLite database is bind-mounted to the host (`./mandate_rescue.db`) so data survives `docker compose down`. The healthcheck (`GET /healthz`) polls every 30 seconds.

**What's in the image:** Gunicorn (`app:app` with `PYTHONPATH=/app/backend`), 2 workers, 4 threads, 120s timeout for SSE streams, non-root user, tini as PID-1. The development server (`python backend/app.py`) is still available for local iteration but should not be used in Docker.

**Important:** Create `mandate_rescue.db` as an empty **file** before running `docker compose up`. Docker creates a directory if the path does not exist, which breaks SQLite. The `touch` above handles this, as does `make up`.

---

## Benchmark

Reproducible three-strategy comparison (run from the project root):

```bash
python benchmark.py --n-runs 30 --seed 42
```

Compares Baseline A (naive, 1 attempt), Baseline B (dumb persistence, 3 attempts, no strategy), and Mandate Rescue (full pipeline). All three use the identical per-attempt probability model — only the strategy differs. Results are Monte Carlo means ± 95% CI using Student's t-distribution.

Example output (seed=42, 30 runs):

```
Strategy              Recovery rate       Recovered Rs      Escal. rate
Baseline A (naive)    46.7% +/-0.0pp      Rs 275,810 +/-Rs0  n/a
Baseline B (persist.) 76.7% +/-0.0pp      Rs 438,916 +/-Rs0  n/a
Mandate Rescue        75.4% +/-0.5pp      Rs 433,516 +/-Rs5k 23.0% +/-0.5pp
```

The negative B→MR delta (~-1.3pp) is expected: the dataset's `mandate_revoked` cases (15%) are immediately escalated by the rule-based pipeline (no retries permitted), while Baseline B retries them blindly, inflating its raw count. The honest measure is the A→MR delta (+28.7pp) which represents everything the intelligent pipeline contributes vs doing nothing. Policy violations (duplicate processing events): 0 across all runs.

---


## ML validation layer

`backend/ml/` is a scikit-learn model that predicts recovery likelihood independently of the rule-based agent. It is **additive and non-decision** — it validates the rule-based scoring, not replaces it.

```bash
python backend/ml/train_model.py   # train + evaluate, writes model.pkl + metrics.json
```

The model competes LogisticRegression vs GradientBoostingClassifier on a stratified 80/20 split; the winner (by ROC-AUC) is saved. SHAP values provide per-case feature contributions shown in the Case detail drawer and the ML Insights tab.

**Important caveat:** The training data (`training_data.csv`) comes from repeated runs of the synthetic simulation. Labels (`recovered`/not) are stochastic (`rng.random() < prob`) — not real payment outcomes. The model therefore learns an approximation of the scoring formula, not real customer behaviour. This is clearly labeled throughout the UI: "additive validation layer — does not drive decisions."

---

## Limitations and honest status

| Feature | Status |
|---|---|
| Payment retry execution | **Simulated** — `rng.random() < prob`, no Razorpay API call |
| Message delivery (SMS/WhatsApp/Email) | **Generated, not sent** — templates only |
| Real Razorpay integration | **Test mode only** — signature verification is real; no live charges |
| RBI pre-debit scheduling | **Timestamp check only** — scheduling is simulated |
| ML training data | **Synthetic** — bootstrapped from simulation runs, not real payments |
| LLM model | `llama-3.1-8b-instant` via Groq (default) — falls back to templates if key absent or rate-limited |
| Multi-worker safety | SQLite single-writer; `busy_timeout=15s` + `BEGIN IMMEDIATE` lock handles brief contention |
| Authentication | Single shared API key; no user accounts, no token rotation |
| Rate limiting | Not implemented; documented for production path |
| Concurrent processing | `BEGIN IMMEDIATE` serialises same-case concurrent workers; not a distributed lock |

---

## Architecture decisions

Key engineering tradeoffs made in this project:

**1. Event-driven pipeline over monolithic request handling.**
Every webhook enters a pipeline of four independent agents (Diagnosis → Triage → Strategy → Communication). Each stage has a single responsibility and writes to an append-only audit log. This makes every decision independently observable and testable without coupling classification to retry logic or narration.

**2. Idempotency at the database layer, not just application code.**
`webhook_events.razorpay_event_id` has a UNIQUE constraint — a duplicate delivery fails at the INSERT, not inside a Python if-check. This prevents double processing even if the application crashes between the check and the write. `BEGIN IMMEDIATE` serialises concurrent workers before the terminal-audit check, covering the race window that pure application-level checks miss.

**3. Explicit state machine with recorded transitions.**
`case_status` follows a defined set of legal transitions (`LEGAL_TRANSITIONS` in `db.py`). The `_RunContext.set_status()` method validates every transition at runtime and appends a row to `state_transitions`. An illegal transition (e.g. `recovered → in_progress`) raises `ValueError` immediately — it does not silently succeed. This makes a class of correctness bugs impossible rather than just unlikely.

**4. AI used for narration, not decisions.**
The LLM (Groq/llama) narrates decisions that the deterministic rule-based engine has already made. It never decides retry/escalation/compliance. This is a deliberate trust boundary: unvalidated LLM output cannot create duplicate charges, skip compliance checks, or affect money totals. The LLM can produce wrong text; the audit trail is always correct.

**5. Two baselines instead of one.**
`run_baseline()` (1 attempt, no strategy) and `run_dumb_persistence_baseline()` (3 attempts, no strategy) give two distinct comparisons. The first answers "did the agent beat doing nothing?" The second answers "did the agent's *strategy* add value beyond just retrying more?" The second is the harder, more defensible claim.

**6. Synthetic simulation alongside Razorpay test-mode, never mixed.**
The 180-case synthetic simulation is seeded (deterministic), labelled `source: synthetic`, and exists purely for demo scale. Real Razorpay events are labelled `source: razorpay_live`. Both flow through the identical recovery pipeline, but they are always distinguishable in the data. The benchmark and chaos suite operate only on the synthetic data; they never touch live-sourced cases.

**7. SQLite with a 15-second busy timeout and single-writer discipline.**
SQLite was chosen to eliminate infrastructure dependencies (no Postgres, no Redis, no message queue) while remaining provably correct for a single-writer workload. Two Gunicorn workers share one writer lock; brief contention is handled by `busy_timeout`. A production deployment would replace SQLite with PostgreSQL and use `SELECT FOR UPDATE` instead of `BEGIN IMMEDIATE`.

---

## Future work

- Replace SQLite with PostgreSQL for multi-worker production deployment
- Implement real Razorpay payment retry API calls (test-mode first)
- Add Razorpay Smart Collect / recurring-charge API for real mandate renewal links
- SMS/WhatsApp delivery via Razorpay Messages or a third-party provider
- Per-user authentication with scoped roles (merchant vs admin)
- Rate limiting on AI query and agent-execution endpoints
- Scheduled retry execution via a proper task queue (Celery / RQ)
- Real ML training dataset from anonymised historical payment outcomes

---

## Project structure

```
Mandate_Rescue/
├── backend/
│   ├── app.py               # Flask app, all API routes, SSE, security middleware
│   ├── db.py                # SQLite access layer (parameterized SQL, no ORM)
│   ├── seed.py              # 180-record synthetic data generator (seed=42)
│   ├── agent.py             # Four-agent pipeline: Diagnosis/Triage/Strategy/Comms
│   ├── scoring.py           # Recoverability score (weighted 0-100)
│   ├── salary_window.py     # Per-customer salary-window inference
│   ├── messaging.py         # Template message generation (Standard + Hinglish)
│   ├── llm_client.py        # LLM wrapper: reasoning, messages, NL-query translation
│   ├── query.py             # NL query execution (parameterized SQL + computed filters)
│   ├── metrics.py           # Dashboard KPI aggregations from real rows
│   ├── baseline.py          # Naive + dumb-persistence baselines
│   ├── health.py            # Subscription health score
│   ├── security.py          # API-key gate
│   ├── webhook_security.py  # Fail-closed HMAC for synthetic demo pipeline
│   ├── razorpay_adapter.py  # Real Razorpay webhook verification + event mapping
│   ├── razorpay_client.py   # Test-mode subscription/plan creation
│   ├── simulation_runner.py # Monte Carlo policy simulation (Policy Sandbox)
│   ├── export.py            # CSV summary export
│   ├── audit_check.py       # 7 re-derived correctness checks (read-only)
│   ├── chaos_test.py        # 10 adversarial scenarios (isolated in-memory DBs)
│   ├── ml/                  # Additive ML validation layer (non-decision)
│   │   ├── train_model.py   # LR vs GBM, stratified split, saves model.pkl
│   │   ├── predict.py       # Lazy-load inference
│   │   └── explain.py       # SHAP per-case + global feature importance
│   └── tests/               # pytest suite (321 tests)
├── benchmark.py             # Reproducible 3-strategy comparison CLI
├── frontend/
│   ├── templates/index.html # SPA shell (accessible, keyboard shortcuts)
│   └── static/
│       ├── style.css        # Design system: Razorpay blue, Inter/JetBrains Mono
│       └── app.js           # Dashboard logic, SSE, Case Replay, Funnel, Inspector
├── scripts/dev/             # Dev/probe scripts (not part of the app)
├── .github/workflows/ci.yml # GitHub Actions: pytest + audit + chaos on every push
├── Dockerfile               # Gunicorn, non-root user, tini, healthcheck
├── docker-compose.yml       # Single-command run with bind-mounted DB
├── requirements.txt         # Flask, scikit-learn, shap, scipy, gunicorn, dotenv
├── requirements-dev.txt     # + pytest, pytest-cov
└── pytest.ini               # testpaths, slow marker
```
