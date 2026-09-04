# Real vs Simulated vs Estimated — Full Audit

> This document provides an exhaustive accounting of every feature in Mandate Rescue,  
> classified as REAL, SIMULATED, or ESTIMATED. No category is blurred.  
> Last reviewed: Stage 0 hardening (September 2026).

---

## Ground Rules

| Label | Definition |
|---|---|
| **REAL** | The system actually performs the operation against real infrastructure. Outcome comes from real API response, real DB row, or real computation on real stored data. |
| **SIMULATED** | The system generates a synthetic outcome. No real external call is made for the operation itself. Always explicitly labelled in code, DB, and UI. |
| **ESTIMATED** | A model or formula produces a probability, score, or projected value. Based on real inputs but the output is not a confirmed outcome. Always carries `data_type: "estimate"` or `[ESTIMATE]` label. |

---

## Razorpay Integration

| Feature | Classification | Evidence |
|---|---|---|
| Webhook signature verification (HMAC-SHA256 over raw body) | **REAL** | `razorpay_adapter.verify_razorpay_signature()` — identical to Razorpay's own spec |
| Webhook reception and 200 response | **REAL** | `app.api_webhook_razorpay()` — real Flask HTTP endpoint |
| Fail-closed on missing/placeholder secret | **REAL** | `razorpay_adapter._secret_bytes()` raises on any known placeholder |
| Subscription creation (`POST /plans`, `POST /subscriptions`) | **REAL** (Test Mode) | `razorpay_client.create_plan()`, `create_subscription()` |
| Payment link creation (`POST /payment_links`) | **REAL** (Test Mode) | `razorpay_client.create_payment_link()` |
| Payment fetch (`GET /payments/{id}`) | **REAL** (Test Mode) | `razorpay_client.fetch_payment()` |
| Payment capture (`POST /payments/{id}/capture`) | **REAL** (Test Mode) | `razorpay_client.capture_payment()` |
| UPI debit trigger (`POST /subscriptions/{id}/charge`) | **NOT POSSIBLE** | Razorpay Test Mode does not expose this endpoint — documented limitation |
| Debit attempt outcome | **SIMULATED** | `ExecutionMode.SIMULATION` — always labelled, never presented as real debit |

---

## Webhook Pipeline

| Feature | Classification | Evidence |
|---|---|---|
| Idempotency guard | **REAL** | `webhook_events.razorpay_event_id UNIQUE` — DB-level constraint |
| Lifecycle tracking (RECEIVED→COMPLETED) | **REAL** | `db.update_webhook_lifecycle()` writes actual DB rows |
| Case creation from webhook | **REAL** | `db.insert_mandate_failure()` — real row in `mandate_failures` |
| Recovery job creation | **REAL** | `db.insert_recovery_job()` — real row in `recovery_jobs` |
| Duplicate delivery handling | **REAL** | Returns 200 DUPLICATE; writes `webhook_duplicate` audit row |

---

## Recovery Pipeline

| Feature | Classification | Evidence |
|---|---|---|
| Failure reason classification | **REAL** | `DiagnosisAgent` — deterministic rule-based, not AI-generated |
| Recoverability score (0–100) | **REAL** | `scoring.compute_score()` — documented formula, not random |
| Health score | **REAL** | `health.compute_health_score()` — computed from stored case fields |
| Strategy selection | **REAL** | `StrategyAgent` — rule-based per failure_reason × merchant_category |
| RBI pre-debit notification gate | **REAL** | `salary_window.py` — enforces ≥24h window before retry |
| Retry cap enforcement | **REAL** | `agent.MAX_RETRIES` check — never retries beyond policy |
| mandate_revoked safety gate | **REAL** | Always `immediate escalation` regardless of any learned data |
| State machine enforcement | **REAL** | `db.LEGAL_TRANSITIONS` + `ValueError` on illegal transition |
| Audit trail | **REAL** | Every decision → `audit_log` row (append-only, never deleted) |

---

## Execution

| Feature | Classification | Evidence |
|---|---|---|
| Recovery jobs (scheduling) | **REAL** | `recovery_jobs` table — durable SQLite rows |
| Scheduler worker (BEGIN IMMEDIATE) | **REAL** | `scheduler.py` — real DB transaction locking |
| REAL_TEST execution path | **REAL** (when credentials set) | `payment_executor.PaymentExecutionService` calls Razorpay API |
| SIMULATION execution path | **SIMULATED** | `ExecutionMode.SIMULATION` — RNG outcome, no API call |
| `CONFIGURATION_ERROR` on missing credentials | **REAL** | Never silently marks simulated job as recovered |

---

## Intelligence and Analytics

| Feature | Classification | Evidence |
|---|---|---|
| Recovery rate (from audit_log) | **REAL** | `metrics.core_metrics()` — counted from real outcome rows |
| Wilson CI on recovery rate | **REAL** (statistically correct) | `scipy.stats` or manual Wilson formula on real n |
| Strategy performance by failure_reason | **REAL** | `intelligence.by_strategy()` — from `audit_log` JOIN |
| Cohort analysis (by tenure / category) | **REAL** | `metrics.cohorts()` — real stored rows |
| Time-series analytics | **REAL** | `data_type: "ACTUAL"` — from `mandate_failures.failure_date` |
| Revenue-at-risk score | **ESTIMATED** | `risk_engine.py` — formula on stored features, labelled `estimate` |
| Economic value E[net_value] | **ESTIMATED** | `economic_value.py` — P(recovery) × amount − costs, labelled |
| ML recovery probability | **ESTIMATED** | `ml/predict.py` — LogisticRegression on synthetic training data |
| Adaptive policy recommendation | **REAL** (on synthetic outcomes) | `adaptive_policy.py` — computes from real `audit_log` rows |
| Anomaly detection | **REAL** | `anomaly_detector.py` — statistical thresholds on real stored data |
| Revenue Investigator answers | **REAL** | `intelligence.py` + `query.py` — parameterized SQL on real data |

---

## Learning System

| Feature | Classification | Evidence |
|---|---|---|
| Strategy performance tracking | **REAL** (on simulation outcomes) | `strategy_performance` table — provenance-tagged |
| A/B experiment framework | **REAL** (infrastructure) | DB tables + deterministic arm assignment exist; no live experiments yet |
| Policy recommendations | **REAL** (on simulation data) | `policy_engine.generate_recommendations()` — evidence-gated (≥10 cases, ≥3pp) |
| Policy version history | **REAL** | `policy_versions` table — immutable after creation |
| Strategy drift detection | **REAL** (on simulation data) | `strategy_drift.py` — compares recent vs historical windows |

---

## Authentication

| Feature | Classification | Evidence |
|---|---|---|
| Merchant registration + OTP | **REAL** | `auth.py` + `email_service.py` — real bcrypt, real OTP generation |
| Email delivery (google_smtp mode) | **REAL** | `GoogleSMTPProvider` — real SMTP call to smtp.gmail.com |
| Email delivery (simulated mode) | **SIMULATED** | `SimulatedEmailProvider` — logs to server, no real email sent |
| Session management | **REAL** | `auth_sessions` table — real 7-day HttpOnly session cookies |
| Security event logging | **REAL** | `db.log_security_event()` — real rows in `security_events` table |

---

## ML Model

| Feature | Classification | Evidence |
|---|---|---|
| Model training | **REAL** | Real scikit-learn training on real CSV data |
| Training data | **SYNTHETIC** | Labels from rule-based pipeline simulation, not real payments |
| Model metrics (precision/recall/AUC) | **REAL** (on synthetic data) | Computed on held-out 20% test split — honest evaluation |
| Model influence on decisions | **NONE** | `predict.py` doc: "Callers must not use this to change any retry/escalation/compliance behavior" |

---

## Scale / Benchmark / Demo

| Feature | Classification | Evidence |
|---|---|---|
| 180-case synthetic seed | **SIMULATED** | `seed.py` — deterministic generator, `source='synthetic'` |
| Monte Carlo policy sandbox | **SIMULATED** | `simulation_runner.py` — RNG, `data_type: "simulation"` |
| Benchmark (3-strategy comparison) | **SIMULATED** | `benchmark.py` — Monte Carlo with 30-run CI |
| Chaos test scenarios | **SIMULATED** | `chaos_test.py` — isolated in-memory DBs, never touch production |

---

## Summary Table

| Category | REAL | SIMULATED | ESTIMATED |
|---|---|---|---|
| Razorpay integration | 6 features | 1 (debit outcome) | 0 |
| Webhook pipeline | 5 features | 0 | 0 |
| Recovery pipeline | 8 features | 0 | 0 |
| Execution | 3 features | 1 | 0 |
| Intelligence / analytics | 8 features | 0 | 3 |
| Learning system | 5 features | 0 | 0 |
| Authentication | 4 features | 1 (simulated email) | 0 |
| ML model | 1 (training) | 1 (training data) | 1 (predictions) |
| Demo / scale | 0 | 4 | 0 |

**No SIMULATED result is presented as REAL anywhere in the system.**  
Every boundary is enforced at the code level (`ExecutionMode`, `source` field, `data_type` label) and visible in the UI.
