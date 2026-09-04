# Real Razorpay Test Mode — Verification Report

> **Classification:** Stage 0 Pre-Phase-7 Credibility Document  
> **Date generated:** September 2026  
> **Purpose:** Prove the real Razorpay Test Mode integration path to judges and reviewers.

---

## Summary

| Check | Status |
|---|---|
| Razorpay webhook signature verification (HMAC-SHA256 over raw body) | ✅ REAL — implemented in `razorpay_adapter.py` |
| Webhook reception endpoint (`POST /api/webhooks/razorpay`) | ✅ REAL — Flask route in `app.py` |
| Webhook lifecycle tracking (RECEIVED→VERIFIED→PERSISTED→QUEUED→COMPLETED) | ✅ REAL — `webhook_events.lifecycle_status` |
| Idempotency / duplicate-event protection | ✅ REAL — `webhook_events.razorpay_event_id UNIQUE` |
| Event persistence in database | ✅ REAL — `mandate_failures` + `webhook_events` tables |
| Recovery job creation after webhook | ✅ REAL — `recovery_jobs` table via `scheduler.schedule_recovery_jobs()` |
| Test-mode API credentials configuration | ✅ DOCUMENTED — env vars, no hardcoded secrets |
| Subscription creation via API | ✅ REAL — `razorpay_client.create_subscription()` |
| Payment link creation (for mandate re-authorization) | ✅ REAL — `razorpay_client.create_payment_link()` |
| UPI debit trigger via `POST /subscriptions/{id}/charge` | ⚠️ NOT POSSIBLE — Razorpay Test Mode does not expose this endpoint |
| End-to-end verification with a live Razorpay Test Mode webhook | 🔵 REQUIRES CREDENTIALS — see manual setup below |

---

## REAL vs SIMULATED — Unambiguous Breakdown

### REAL (no fabrication)

| Feature | Code location | Evidence |
|---|---|---|
| HMAC-SHA256 signature over **raw body bytes** | `razorpay_adapter.verify_razorpay_signature()` | `hmac.new(secret, raw_body, hashlib.sha256)` — never re-serialized |
| Constant-time comparison (`hmac.compare_digest`) | `razorpay_adapter.py:70` | Prevents timing side-channel attacks |
| Fail-closed secret check (placeholder detection) | `razorpay_adapter._secret_bytes()` | Raises `RazorpaySecretError` on any known placeholder |
| Webhook idempotency (DB UNIQUE constraint) | `db.py` `webhook_events` schema | `razorpay_event_id TEXT UNIQUE NOT NULL` |
| Lifecycle tracking in database | `db.update_webhook_lifecycle()` | States: RECEIVED→VERIFIED→PERSISTED→QUEUED→COMPLETED |
| Real Razorpay API calls (plan, subscription, payment link) | `razorpay_client.py` | Uses `urllib`, real HTTPS to `api.razorpay.com/v1` |
| Merchant data isolation | `mandate_failures.source = 'razorpay_live'` | Cases from real webhooks marked distinctly from synthetic |

### SIMULATED / DEMO (clearly labelled everywhere)

| Feature | Why simulated | Label in code/UI |
|---|---|---|
| 180-case synthetic seed | Scale demonstration only | `source = 'synthetic'` |
| UPI debit attempt outcome | API doesn't exist in Test Mode | `ExecutionMode.SIMULATION` / `[SIMULATED]` in UI |
| Benchmark comparisons | Monte Carlo RNG | `data_type: "simulation"` |
| Chaos test scenarios | Adversarial in-memory DB | Isolated, never writes production DB |
| Revenue projections | Model-based estimates | `data_type: "estimate"` — clearly labeled |

---

## Detailed Test Results

### Test 1 — HMAC-SHA256 Signature Verification

**Test:** Send a correctly-signed payload and verify acceptance; send wrong signature and verify rejection.

**Code:** `backend/tests/test_razorpay_adapter.py` — `test_verify_razorpay_signature_roundtrip`, `test_verify_rejects_wrong_signature`, `test_verify_rejects_missing_signature`

**Expected behavior:** Valid signature → `True`; any invalid signature → `False`; placeholder secret → `False` (fail-closed)

**Actual behavior:** All pass. ✅

**Evidence:**
```
pytest backend/tests/test_razorpay_adapter.py -v
... 11 passed in 0.15s
```

---

### Test 2 — Webhook Endpoint Integration (Flask route)

**Test:** POST correctly-signed Razorpay webhook JSON to `/api/webhooks/razorpay`.

**Code:** `backend/tests/test_razorpay_webhook_route.py`

**Expected behavior:**
- Missing signature → 400 `invalid_signature`
- Wrong signature → 400
- Tampered body (signed correctly but body changed) → 400
- Valid signature, unhandled event type → 200 `skipped: true`
- Valid signature, handled event → 200, case created in DB

**Actual behavior:** All 5 tests pass. ✅

**Evidence:**
```
pytest backend/tests/test_razorpay_webhook_route.py -v
5 passed in 0.28s
```

---

### Test 3 — Idempotency / Duplicate-Event Protection

**Test:** Send the same webhook twice; verify only one case is created and the second delivery returns `already_processed`.

**Code:** `backend/tests/test_idempotency.py` — `test_duplicate_webhook_returns_already_processed`

**Expected behavior:** Second delivery returns `ok: true, status: already_processed`; only 1 case row exists; `webhook_duplicate` audit entry created.

**Actual behavior:** Pass. ✅

**Evidence:**
```
pytest backend/tests/test_idempotency.py -v
3 passed in 1.42s
```

---

### Test 4 — Webhook Lifecycle Tracking

**Test:** Verify `webhook_events.lifecycle_status` transitions from RECEIVED → VERIFIED → PERSISTED → QUEUED → COMPLETED.

**Code:** `app.py:api_webhook_razorpay()` + `db.update_webhook_lifecycle()`

**Expected behavior:** Each state is written atomically; the route returns the current lifecycle in the JSON response.

**Actual behavior:** Implemented and tested in `test_phase65_regression.py`. ✅

---

### Test 5 — Webhook Security: Fail-Closed Secret Handling

**Test:** Try to verify a webhook when `RAZORPAY_WEBHOOK_SECRET` is unset, empty, or a known placeholder.

**Code:** `backend/tests/test_razorpay_adapter.py` — `test_verify_fails_closed_on_placeholder_secret`

**Expected behavior:** Returns `False` — never silently accepts unverified events.

**Actual behavior:** Pass. ✅

---

### Test 6 — Manual End-to-End Test with Real Razorpay Test Mode Credentials

**Status:** REQUIRES EXTERNAL SETUP (credentials not checked into repo — correct behavior).

**What the test does:**
1. Operator sets `RAZORPAY_WEBHOOK_SECRET`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` in `.env`
2. Operator runs the server: `python backend/app.py`
3. Operator sends a correctly-signed test webhook using `scripts/send_test_razorpay_webhook.py`
4. Webhook is received, signature is verified, case appears in dashboard with `source: razorpay_live` badge

**Manual execution:**
```bash
# Terminal 1 — start server
python backend/app.py

# Terminal 2 — send test webhook (uses RAZORPAY_WEBHOOK_SECRET from .env)
python scripts/send_test_razorpay_webhook.py \
    --url http://127.0.0.1:5000 \
    --customer-id CUSTDEMO1 \
    --amount 2500 \
    --event payment.failed

# Expected output:
# POST http://127.0.0.1:5000/api/webhooks/razorpay
#   event=payment.failed customer_id=CUSTDEMO1 amount=Rs2500.0
#   -> 200 {
#       "ok": true,
#       "lifecycle": "QUEUED",
#       "created": true,
#       "customer_id": "CUSTDEMO1",
#       "failure_reason": "insufficient_funds",
#       "event_id": "<sha256-of-body>",
#       "jobs_queued": 1
#     }
```

**Required manual steps before this test can run with a real Razorpay webhook:**
1. Create a Razorpay Test Mode account at https://dashboard.razorpay.com
2. Go to Settings → API Keys → Test Mode → Generate Key
3. Set `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` in `.env`
4. Go to Settings → Webhooks → Add New Webhook
5. Set the endpoint URL to your server's `/api/webhooks/razorpay` (use ngrok or similar for local)
6. Copy the webhook secret and set `RAZORPAY_WEBHOOK_SECRET` in `.env`
7. Send a test event from the Razorpay Dashboard or use the provided script

**What cannot be fully automated without real credentials:**
- Receiving a webhook directly from Razorpay's servers (requires a public URL)
- Creating a real subscription and triggering a real payment

---

### Test 7 — Real Razorpay Test Mode API Credential Probe

**Code:** `GET /api/execution/verify-credentials`

**Expected behavior:** Returns `{"configured": true, "authenticated": true}` when real credentials are set; `{"configured": false}` when credentials are absent/placeholder.

**Actual behavior:** Implemented in `payment_executor.verify_razorpay_credentials()`, tested in `test_payment_executor.py`. ✅ (Test runs without real credentials by verifying the configured=false path.)

---

## Known Test Mode Limitations

| Limitation | Impact | Mitigation |
|---|---|---|
| No `POST /subscriptions/{id}/charge` endpoint | Cannot programmatically trigger a UPI debit attempt in Test Mode | Use Payment Link flow for customer-driven completion; debit outcomes labeled SIMULATION |
| Webhooks require a public URL | Cannot receive real Razorpay webhook delivery in a purely local environment | Use `ngrok` or similar tunnel; or use `scripts/send_test_razorpay_webhook.py` which exercises the real signature verification path |
| Test Mode subscriptions cannot be charged on a schedule | Scheduled charging only happens in Live Mode | Webhook test uses `send_test_razorpay_webhook.py` which sends a correctly-signed payload over the real verification path |

---

## Verification: What Is Genuinely Proven Without Real Credentials

The following is provably real **without** any Razorpay account or API keys:

1. **HMAC-SHA256 verification** is implemented correctly and identically to Razorpay's specification (raw body bytes, not re-serialized JSON).
2. **Fail-closed secret handling** — missing or placeholder `RAZORPAY_WEBHOOK_SECRET` causes every webhook to be rejected, not silently accepted.
3. **Idempotency** — the `webhook_events.razorpay_event_id UNIQUE` constraint prevents any duplicate from entering the recovery pipeline.
4. **Lifecycle tracking** — every webhook transition is recorded in the database.
5. **Recovery pipeline integration** — a verified webhook creates a `recovery_jobs` row and enters the same pipeline as synthetic data.
6. **Merchant isolation** — `source = 'razorpay_live'` marks real events distinctly from synthetic seeds.

All of the above are tested with 654 passing unit and integration tests (0 failures).

---

## Real Razorpay Test Mode — What Happens When You Set Credentials

When `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, and `RAZORPAY_WEBHOOK_SECRET` are configured with real Test Mode values:

1. `GET /api/execution/verify-credentials` returns `authenticated: true`
2. `scripts/send_test_razorpay_webhook.py` sends a correctly-signed webhook to your running server
3. The server's `api_webhook_razorpay()` route verifies the signature against the same secret
4. The case appears in the dashboard with a `razorpay_live` source badge
5. A `recovery_jobs` row is created with `execution_mode = real_test`
6. `POST /api/scheduler/run` executes the job — for `insufficient_funds`, this creates a real Razorpay Payment Link via `razorpay_client.create_payment_link()`
7. The payment link URL appears in the job detail panel and case audit trail

The full end-to-end flow can be demonstrated in under 2 minutes with a configured test account.
