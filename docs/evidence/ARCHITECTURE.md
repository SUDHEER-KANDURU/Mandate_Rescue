# Mandate Rescue — Technical Architecture

> Evidence document for Stage 0 judge review.  
> Every claim here maps to a specific file and line in the repository.

---

## System Overview

Mandate Rescue is an event-driven payment recovery platform. Its job is to detect failed recurring payments (UPI Autopay / NACH e-mandates), diagnose the failure, choose the best recovery action, execute it, track the outcome, and learn what works over time.

The key design principle: **every decision is traceable**. From the moment a webhook arrives to the final recovered/escalated outcome, every step is a row in the database with a timestamp, actor, and reason.

---

## Component Map

```
┌────────────────────────────────────────────────────────────┐
│  INGESTION                                                 │
│                                                            │
│  POST /api/webhooks/razorpay  (real Razorpay events)       │
│  POST /api/seed               (synthetic 180-case demo)    │
│                                                            │
│  razorpay_adapter.py   — HMAC-SHA256 verify (raw bytes)    │
│  webhook_security.py   — HMAC for synthetic pipeline       │
│  db.webhook_events     — idempotency (UNIQUE event_id)     │
│  db.mandate_failures   — case storage                      │
└──────────────────────────────┬─────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────┐
│  RECOVERY ORCHESTRATION                                    │
│                                                            │
│  agent.RecoveryPipeline.process_case()                     │
│                                                            │
│  1. DiagnosisAgent      — classifies failure reason        │
│                           validates case status (FSM)      │
│                                                            │
│  2. TriageAgent         — computes recoverability 0–100    │
│                           scoring.compute_score()          │
│                           health.compute_health_score()    │
│                                                            │
│  3. StrategyAgent       — selects recovery action          │
│                           per failure_reason × merchant    │
│                           RBI pre-debit / retry-cap gates  │
│                           adaptive_policy recommendation   │
│                                                            │
│  4. CommunicationAgent  — generates dunning messages       │
│                           messaging.py (Standard+Hinglish) │
│                           llm_client.py (narration only)   │
│                                                            │
│  audit_log              — append-only, every decision      │
│  state_transitions      — FSM history                      │
└──────────────────────────────┬─────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────┐
│  EXECUTION LAYER                                           │
│                                                            │
│  scheduler.schedule_recovery_jobs()                        │
│  db.recovery_jobs  (durable queue, idempotency_key UNIQUE) │
│                                                            │
│  payment_executor.PaymentExecutionService                  │
│    ExecutionMode.REAL_TEST  → Razorpay Test API            │
│      razorpay_client.create_payment_link()                 │
│      razorpay_client.capture_payment()                     │
│      razorpay_client.fetch_subscription()                  │
│    ExecutionMode.SIMULATION → RNG outcome (labelled)       │
│                                                            │
│  salary_window.py  — RBI-compliant retry timing            │
│  scheduler.reset_stale_claimed_jobs()  — crash safety      │
└──────────────────────────────┬─────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────┐
│  INTELLIGENCE + ANALYTICS                                  │
│                                                            │
│  risk_engine.py      — revenue-at-risk scores (estimate)   │
│  anomaly_detector.py — 6 statistical detectors             │
│  intelligence.py     — strategy/failure-reason analytics   │
│  adaptive_policy.py  — data-driven strategy recommendation │
│  economic_value.py   — E[net_value] per intervention       │
│  ml/predict.py       — P(recovery) model (additive only)   │
│  ml/explain.py       — SHAP per-case + global importance   │
│  query.py            — NL → parameterized SQL              │
└──────────────────────────────┬─────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────┐
│  CLOSED-LOOP LEARNING                                      │
│                                                            │
│  outcome_attribution.py — writes strategy_performance      │
│  segment_learning.py    — fallback hierarchy               │
│  policy_engine.py       — evidence-gated recommendations   │
│  strategy_drift.py      — detects performance degradation  │
│  experimentation.py     — A/B experiment management        │
│  experiment_evaluator.py — two-proportion z-test           │
│                                                            │
│  DB tables: strategy_performance, experiments,             │
│    experiment_assignments, experiment_outcomes,            │
│    policy_versions, policy_recommendations,                │
│    policy_audit_log, policy_performance                    │
└────────────────────────────────────────────────────────────┘
```

---

## Data Flow: Real Razorpay Webhook

```
Razorpay Dashboard sends POST to /api/webhooks/razorpay
    │
    ├─ 1. raw_body = request.get_data()              # never re-serialized
    ├─ 2. razorpay_adapter.verify_razorpay_signature(raw_body, header)
    │       hmac.new(RAZORPAY_WEBHOOK_SECRET, raw_body, sha256)
    │       hmac.compare_digest(expected, header)    # constant-time
    │       → 400 on failure
    │
    ├─ 3. json.loads(raw_body)                       # parse only after verify
    ├─ 4. razorpay_adapter.map_razorpay_event(payload)
    │       maps event type → failure_reason
    │       extracts customer_id from subscription.notes
    │       builds internal case record
    │
    ├─ 5. razorpay_adapter.claim_webhook_event(conn, payload, raw_body)
    │       db.insert_webhook_event()                # UNIQUE razorpay_event_id
    │       → 200 DUPLICATE if already seen
    │
    ├─ 6. db.insert_mandate_failure(conn, record)
    │       source = 'razorpay_live'
    │       db.update_webhook_lifecycle(event_id, 'PERSISTED')
    │
    ├─ 7. scheduler.schedule_recovery_jobs(conn, case, exec_mode)
    │       db.insert_recovery_job()                 # idempotency_key UNIQUE
    │       db.update_webhook_lifecycle(event_id, 'QUEUED')
    │
    └─ 8. return 200 immediately                     # < 10ms
           { ok, lifecycle: "QUEUED", customer_id, jobs_queued }

    ... async ...

    scheduler worker picks up job
    payment_executor runs REAL_TEST or SIMULATION
    outcome persisted to recovery_jobs + audit_log
    db.update_webhook_lifecycle(event_id, 'COMPLETED')
```

---

## State Machine (case_status)

```
        ┌──────┐
        │ new  │  ← initial state (all seeded/incoming cases)
        └──┬───┘
           │
    ┌──────┴──────┐
    │ in_progress │  ← DiagnosisAgent validates and advances
    └──────┬──────┘
           │
     ┌─────┼──────────┬──────────────┐
     ▼     ▼          ▼              ▼
recovered escalated promised   broken_promise
  (terminal) (terminal) │           │
                        └─────┬─────┘
                              ▼
                         (back to in_progress
                          or recovered/escalated)

  rejected  ← DiagnosisAgent rejects invalid/duplicate
  invalid   ← structural validation failure
```

Legal transitions enforced in `db.LEGAL_TRANSITIONS`.  
Every transition recorded in `state_transitions` table.  
Illegal transitions raise `ValueError` immediately — never silently accepted.

---

## Idempotency Design

Three independent idempotency guards:

| Guard | Mechanism | Scope |
|---|---|---|
| Webhook delivery | `webhook_events.razorpay_event_id UNIQUE` | Prevents duplicate case creation from Razorpay retries |
| Recovery job | `recovery_jobs.idempotency_key UNIQUE` | `customer_id:attempt_number` — prevents double-scheduling |
| Agent pass | `audit_log` terminal-status check in `process_case()` | Prevents re-scoring a finished case |

All three are database-level constraints, not application-level flags. A crash between check and write cannot create a duplicate — the constraint fires on commit.

---

## Webhook Lifecycle Tracking

Every Razorpay webhook progresses through these states in `webhook_events.lifecycle_status`:

```
RECEIVED    webhook body received, before verification
VERIFIED    HMAC-SHA256 signature passed
PERSISTED   case row created/updated in mandate_failures
QUEUED      recovery_jobs row created
PROCESSING  scheduler worker claimed the job
COMPLETED   execution finished, outcome persisted
FAILED      execution or persistence error
DUPLICATE   event_id already existed in webhook_events
REJECTED    signature invalid or event type unhandled
```

Queryable via `GET /api/webhook-events` (returns all events with current lifecycle state).

---

## Execution Modes

Two mutually exclusive modes, determined at job scheduling time and locked into the job row:

```python
class ExecutionMode(Enum):
    REAL_TEST  = "real_test"    # calls Razorpay Test API
    SIMULATION = "simulation"   # RNG-based, no HTTP calls
```

A `SIMULATION` job never silently runs as `REAL_TEST`.  
A `REAL_TEST` job with missing credentials fails with `CONFIGURATION_ERROR` — not silently marked recovered.  
The mode is stored in `recovery_jobs.execution_mode` and visible in the dashboard.

---

## LLM Layer

The LLM is a **narration layer only** — it never drives a recovery decision:

```
Rule-based decision already made
    ↓
CommunicationAgent calls llm_client.chat()
    ↓
LLM adds contextual phrasing to a pre-decided message
    ↓
Template fallback if LLM unavailable/rate-limited
    ↓
Message stored in audit_log.reasoning_text
```

Provider chain: Groq → NVIDIA NIM → OpenAI (configurable order via `LLM_PROVIDER_ORDER`).  
`LLM_LIVE_TOP_N=5` by default — only the top 5 cases by amount use live LLM to stay within Groq free-tier limits.

---

## Security Architecture

```
Inbound webhook          → HMAC-SHA256 raw body, constant-time compare, fail-closed
Mutating API endpoints   → X-API-Key gate (security.py)
SSE stream               → one-use 60-second token (EventSource cannot send headers)
Merchant auth            → bcrypt passwords, 6-digit OTP, 7-day HttpOnly session cookie
SQL queries              → parameterized throughout — no f-string SQL
NL query (ASK)           → LLM output filtered via hardcoded field whitelist before SQL
Secrets                  → env vars only, placeholder detection, never logged
Security headers         → CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy
Rate limiting            → sliding-window per IP on LLM + agent + auth endpoints
```

---

## Database Design Principles

- **Append-only audit trail** — `audit_log` rows are never updated or deleted
- **State machine enforcement** — `LEGAL_TRANSITIONS` dict + runtime `ValueError` on violations
- **Parameterized SQL only** — no string interpolation of user input anywhere in the codebase
- **Busy timeout** — `PRAGMA busy_timeout = 15000` prevents "database is locked" under concurrent load
- **FK enforcement** — `PRAGMA foreign_keys = ON` on every connection
- **Idempotent migrations** — `ALTER TABLE … ADD COLUMN IF NOT EXISTS` pattern; safe to run on old DBs

---

## Performance

Optimizations applied after measurement (Phase 5):

| Operation | Before | After | Change |
|---|---|---|---|
| `/api/cases` (180 rows + ML scores) | ~6,500 ms | 13 ms | Batch ML inference replacing N individual DataFrame constructions |
| `/api/exceptions` | ~14 ms (N+1) | 0.9 ms | Single JOIN query |
| `/api/rejected-webhooks` | ~5 ms (N+1) | 0.2 ms | Single JOIN query |
| `/api/activity` | full table scan | 0.5 ms | `ORDER BY event_id DESC LIMIT 40` + index |
| Webhook HTTP response | 500ms–2s | < 10 ms | Async: persist+enqueue only, pipeline runs later |
| ML cold-start | 3.5s per first request | 0 (eliminated) | Background thread warm-up at import |
