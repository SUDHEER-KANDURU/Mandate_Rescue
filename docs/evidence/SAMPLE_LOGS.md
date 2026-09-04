# Sample Sanitized Logs

> All values below are **sanitized examples** — no real API keys, secrets, customer data, or merchant credentials appear here.  
> Correlation IDs, event IDs, and customer IDs are representative placeholders.

---

## 1. Server Startup

```
2026-09-04 10:00:01 INFO  mandate_rescue.config [-]:
  startup config:
    WEBHOOK_SECRET      = [SET — 64 char hex]
    RAZORPAY_WEBHOOK_SECRET = [SET — 40+ char]
    RAZORPAY_KEY_ID     = rzp_test_[REDACTED]
    MANDATE_RESCUE_API_KEY = [SET — auto-generated]
    GROQ_API_KEY        = [SET]
    LLM_LIVE_TOP_N      = 5
    NOTIFICATION_EMAIL_PROVIDER = simulated

2026-09-04 10:00:01 INFO  mandate_rescue.app [-]: DB initialized
2026-09-04 10:00:01 INFO  mandate_rescue.app [-]: Stale jobs reset (0 affected)
```

---

## 2. Real Razorpay Webhook — Full Lifecycle

```
# RECEIVED
2026-09-04 10:01:15 INFO  mandate_rescue.app [a3f2c1d8]:
  webhook correlation_id=a3f2c1d8 RECEIVED

# VERIFIED (signature passed)
2026-09-04 10:01:15 INFO  mandate_rescue.razorpay_adapter [a3f2c1d8]:
  signature verified for event=payment.failed

# PERSISTED + QUEUED
2026-09-04 10:01:15 INFO  mandate_rescue.app [a3f2c1d8]:
  webhook correlation_id=a3f2c1d8 event_id=evt_[REDACTED]
  customer_id=CUST_DEMO_001 lifecycle=QUEUED
  jobs_created=1 case_created=True exec_mode=real_test

# HTTP response — returned in < 10ms
# { "ok": true, "lifecycle": "QUEUED", "customer_id": "CUST_DEMO_001",
#   "failure_reason": "insufficient_funds", "jobs_queued": 1 }
```

---

## 3. Rejected Webhook — Invalid Signature

```
2026-09-04 10:02:30 WARNING mandate_rescue.app [b7e1a9c2]:
  webhook correlation_id=b7e1a9c2 REJECTED invalid_signature

# HTTP 400 returned
# { "ok": false, "error": "invalid_signature" }
```

---

## 4. Duplicate Webhook — Idempotency Guard

```
2026-09-04 10:01:20 INFO  mandate_rescue.app [d4f8b3e1]:
  webhook correlation_id=d4f8b3e1 event_id=evt_[REDACTED] DUPLICATE

# HTTP 200 returned (Razorpay expects 2xx on redeliveries)
# { "ok": true, "lifecycle": "DUPLICATE", "status": "already_processed" }
```

---

## 5. Recovery Pipeline — Agent Run (one case)

```
2026-09-04 10:05:00 INFO  mandate_rescue.agent [-]:
  [CUST_DEMO_001] DiagnosisAgent: reason=insufficient_funds status=new→in_progress

2026-09-04 10:05:00 INFO  mandate_rescue.agent [-]:
  [CUST_DEMO_001] TriageAgent: score=72 health=healthy tenure=8mo success_rate=0.72

2026-09-04 10:05:00 INFO  mandate_rescue.agent [-]:
  [CUST_DEMO_001] StrategyAgent: strategy=retry_at_salary_window
  amount=2500 mandate_limit=5000 dunning_stage=0 max_retries=3

2026-09-04 10:05:00 INFO  mandate_rescue.agent [-]:
  [CUST_DEMO_001] CommunicationAgent: message=hinglish_nudge1 llm=template_fallback

2026-09-04 10:05:00 INFO  mandate_rescue.agent [-]:
  [CUST_DEMO_001] status=recovered attempts=1
```

---

## 6. Scheduler Worker — REAL_TEST Execution

```
2026-09-04 10:30:00 INFO  mandate_rescue.scheduler [-]:
  worker claiming job_id=job_[REDACTED] customer=CUST_DEMO_001
  attempt=1 mode=real_test amount=2500

2026-09-04 10:30:01 INFO  mandate_rescue.payment_executor [-]:
  REAL_TEST: creating payment_link amount=2500 customer=CUST_DEMO_001
  razorpay_payment_link_id=plink_[REDACTED]
  short_url=https://rzp.io/[REDACTED]

2026-09-04 10:30:01 INFO  mandate_rescue.scheduler [-]:
  job_id=job_[REDACTED] outcome=PAYMENT_LINK_CREATED status=succeeded
```

---

## 7. Scheduler Worker — SIMULATION Execution

```
2026-09-04 10:30:05 INFO  mandate_rescue.scheduler [-]:
  worker claiming job_id=job_[REDACTED] customer=CUST001
  attempt=1 mode=simulation amount=1500

2026-09-04 10:30:05 INFO  mandate_rescue.payment_executor [-]:
  SIMULATION: synthetic outcome=RECOVERED [NOT a real Razorpay debit]

2026-09-04 10:30:05 INFO  mandate_rescue.scheduler [-]:
  job_id=job_[REDACTED] outcome=RECOVERED [SIMULATED] status=succeeded
```

---

## 8. Credential Probe — Razorpay Credentials Not Configured

```
2026-09-04 09:58:00 WARNING mandate_rescue.razorpay_adapter [-]:
  Rejecting Razorpay webhook: RAZORPAY_WEBHOOK_SECRET is not configured
  with a real secret. No real Razorpay event can be verified until this is fixed.
```

---

## 9. LLM Rate-Limit Fallback

```
2026-09-04 10:05:02 WARNING mandate_rescue.llm_client [-]:
  Groq rate-limited (429). Falling back to NVIDIA NIM.

2026-09-04 10:05:04 WARNING mandate_rescue.llm_client [-]:
  NVIDIA NIM failed (timeout). Falling back to template narration.

2026-09-04 10:05:04 INFO  mandate_rescue.llm_client [-]:
  Using template fallback for customer CUST002 — narration is deterministic.
```

---

## 10. Anomaly Detection Alert

```
2026-09-04 10:10:00 INFO  mandate_rescue.anomaly_detector [-]:
  anomaly detected: failure_rate_spike
  segment=insufficient_funds rate=0.52 overall=0.38 deviation=+37%
  severity=high data_type=actual
```

---

## Important Notes

- **No secret values appear in any log line** — secrets are only confirmed as `[SET]` or `[NOT SET]`
- **No customer PII appears** — customer IDs are synthetic identifiers
- **SIMULATION outcomes are always explicitly labeled** — `[SIMULATED]`, `[NOT a real Razorpay debit]`
- **Correlation IDs** (`X-Correlation-ID`) link every request across all log lines
- **Log level** is configurable via `LOG_LEVEL` env var (DEBUG/INFO/WARNING/ERROR)
