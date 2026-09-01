# Mandate Rescue

[![CI](https://github.com/OWNER/Mandate_Rescue/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/Mandate_Rescue/actions/workflows/ci.yml)

AI-driven recovery for failed UPI Autopay / e-mandate auto-debits on a Razorpay
merchant platform. Instead of one generic reminder per failure, the agent diagnoses the
failure type, scores how recoverable each case is, applies a tailored strategy, and keeps
an honest, fully-traceable audit trail — all surfaced on a clean operations dashboard.

The recovery flow runs as an explicit **four-agent pipeline**
(Diagnosis → Triage → Strategy → Communication). See [ARCHITECTURE.md](ARCHITECTURE.md)
for the diagram and stage-by-stage responsibilities.

**This isn't just a simulation of the idea.** `/api/webhooks/razorpay` is a real
Razorpay webhook receiver — it verifies Razorpay's actual HMAC-SHA256 signature
scheme over the raw request body and feeds genuine Razorpay test-mode events
through the *exact same* recovery pipeline as the 180-case synthetic demo. See
[Real Razorpay integration](#real-razorpay-integration) below.

## Project structure

```
Mandate_Rescue/
├── backend/                 # Python Flask backend + recovery engine
│   ├── app.py               # Flask entrypoint + JSON API (incl. SSE live run, NL ask)
│   ├── db.py                # SQLite access layer (parameterized SQL, no ORM)
│   ├── seed.py              # 180-record synthetic data generator (fixed seed)
│   ├── scoring.py           # recoverability score (R6)
│   ├── salary_window.py     # per-customer salary-window inference (R8)
│   ├── messaging.py         # template message generation, Standard + Hinglish (R9)
│   ├── llm_client.py        # LLM wrapper: reasoning, messages, NL-query translation
│   ├── query.py             # structured NL-query execution (real parameterized SQL)
│   ├── agent.py             # four-agent pipeline + compliance layer (R1-R4, R13-R16)
│   ├── metrics.py           # dashboard aggregations from audit_log (R5)
│   ├── baseline.py          # naive + "dumb persistence" baselines (R11)
│   ├── export.py            # CSV summary export (R12)
│   ├── health.py            # subscription health score (R17)
│   ├── security.py          # internal API-key gate for mutating endpoints
│   ├── webhook_security.py  # fail-closed HMAC signing/verification (synthetic demo path)
│   ├── razorpay_adapter.py  # REAL Razorpay webhook verification + event mapping
│   ├── razorpay_client.py   # thin REST client for creating real test-mode subscriptions
│   ├── audit_check.py       # correctness audit: 7 re-derived business-rule checks
│   ├── chaos_test.py        # adversarial test suite: 7 attack scenarios
│   └── tests/               # pytest suite wrapping the above + unit tests
├── frontend/                # Dashboard UI (vanilla HTML/CSS/JS)
│   ├── templates/index.html
│   └── static/{style.css, app.js}
├── scripts/dev/             # one-off dev/screenshot/probe scripts (not part of the app)
├── docs/screenshots/        # dashboard screenshots
├── .github/workflows/ci.yml # GitHub Actions: pytest + audit + chaos suite on every push
├── Dockerfile, docker-compose.yml  # one-command containerized run
├── ARCHITECTURE.md          # four-agent pipeline diagram (Mermaid)
├── AUDIT_AND_UPGRADE_PLAN.md # build log / self-audit for this upgrade pass
├── pytest.ini, conftest.py  # test configuration
├── mandate_rescue.db        # generated at runtime (gitignored)
├── requirements.txt, requirements-dev.txt
└── README.md
```

## Setup & run

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in a real WEBHOOK_SECRET (see below) — required
python backend/app.py
```

Then open http://127.0.0.1:5000. Use **Reset demo** to seed fresh data, then **Run agent**
to watch cases stream through the pipeline live.

### Or run with Docker

```bash
docker compose up --build
```

Serves the same app on http://127.0.0.1:5000, with a healthcheck at `/healthz`. Reads
secrets from your local `.env` (see `.env.example`); the SQLite file is bind-mounted
to the host so demo data survives `docker compose down`.

### Required: WEBHOOK_SECRET

Webhook signature verification is **fail-closed** — there is no insecure default. Set
a real secret before running anything that touches the webhook pipeline (seeding,
running the agent, or the Razorpay route):

```powershell
python -c "import secrets; print(secrets.token_hex(32))"   # generate one
# paste the result as WEBHOOK_SECRET=... in your .env
```

Without a valid secret, every webhook (synthetic or real) is rejected rather than
silently trusted against a publicly-known placeholder — see `webhook_security.py`.

### Optional: real LLM narration

The reasoning text, nudge messages, and the "Ask the data" box use an LLM when a key is
present, and fall back to deterministic templates otherwise. No extra pip package is
required (the client uses the standard library).

```powershell
$env:GROQ_API_KEY = "gsk_..."      # Groq (OpenAI-compatible) key
# optional overrides:
$env:LLM_MODEL     = "llama-3.1-8b-instant"
$env:LLM_API_BASE  = "https://api.groq.com/openai/v1"
$env:LLM_TIMEOUT   = "8"
```

**Without a key the app runs completely** — every LLM feature degrades gracefully to
template text, and the NL query box shows a "try an example" prompt instead of erroring.

## Real Razorpay integration

Unlike a purely synthetic demo, `POST /api/webhooks/razorpay` is a real webhook
receiver that speaks Razorpay's actual protocol:

- **Verification** (`razorpay_adapter.verify_razorpay_signature`) recomputes
  HMAC-SHA256 over the **raw request body bytes** (never a re-serialized copy, which
  would silently break the signature) and compares it in constant time against the
  `X-Razorpay-Signature` header, keyed with `RAZORPAY_WEBHOOK_SECRET` — the secret
  you configure in the Razorpay Dashboard under Settings → Webhooks. This is a
  separate secret and a separate scheme from the synthetic demo path's
  `WEBHOOK_SECRET` / `webhook_security.py`.
- **Mapping** (`razorpay_adapter.map_razorpay_event`) turns Razorpay's real nested
  webhook payload (`payload.subscription.entity`, `payload.payment.entity`) into
  this project's internal case shape, then inserts it with the same
  `db.insert_mandate_failure` used for synthetic data.
- **Identical downstream pipeline.** A real Razorpay-sourced case flows through the
  exact same `DiagnosisAgent → TriageAgent → StrategyAgent → CommunicationAgent`
  pipeline as the synthetic seed — same scoring, same retry cap, same RBI compliance
  check. It's tagged `source: "razorpay_live"` (vs. `"synthetic"`) purely for display,
  and shown with a distinct badge in the dashboard's Cases table and a dedicated
  "Real Razorpay webhook intake" card on the Overview.
- **Setting up real test-mode subscriptions**: `razorpay_client.py` is a thin REST
  client (stdlib `urllib`, no extra dependency) for creating a Razorpay **test-mode**
  plan/subscription via `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` (Dashboard → Settings
  → API Keys). Point Razorpay's webhook configuration (using a tunnel like ngrok for
  local testing) at `/api/webhooks/razorpay` and a real test payment failure will flow
  into the live dashboard.

Razorpay's webhook signing scheme is documented at
[razorpay.com/docs/webhooks](https://razorpay.com/docs/webhooks/) — HMAC-SHA256
hex-encoded over the raw body, keyed with the dashboard-configured webhook secret.

## The four-agent pipeline

1. **DiagnosisAgent** — classifies the incoming webhook event into a `failure_reason`
   and `raw_event_type` (R13).
2. **TriageAgent** — computes the recoverability score (R6) and subscription health
   score (R17) and decides processing order (highest value first).
3. **StrategyAgent** — applies the per-reason strategy, the UPI mandate-limit gate
   (R14), the hard 3-retry cap (R2), the promise-to-pay flow (R3), and the RBI
   pre-debit compliance check (R15). This stage owns every decision.
4. **CommunicationAgent** — generates the LLM-narrated reasoning and nudge messages and
   manages the Day 1 / Day 3 / Day 7 dunning sequence (R9, R16). Narration only.

> **Trust boundary.** The LLM narrates decisions and interprets query intent; it never
> makes a decision or invents data. Scores, strategy, retry cap, compliance status, and
> dunning stage are all deterministic rule-based code. The natural-language query box
> uses the LLM only to translate a question into filter parameters, which are then run
> as real parameterized SQL against the database.

## Natural-language query ("Ask the data")

Type a question like *"show me all non-compliant high-value cases"* or *"which customers
are at high churn risk"*. The LLM translates it into a small JSON filter spec (e.g.
`{"compliance_status": "non-compliant", "amount_min": 5000}`); `query.py` executes that
spec as parameterized SQL plus the same real scoring/health functions used everywhere
else, and returns the matching real cases with a one-line summary. Invalid or empty
questions get a graceful "try one of the examples" response.

## API endpoints

Endpoints marked 🔒 require an `X-API-Key` header (see [Security hardening](#security-hardening)); the dashboard UI sends this automatically.

- `POST /api/reset` 🔒 — re-seed fresh data + clear the previous run and LLM cache
- `POST /api/seed` 🔒 — (re)generate the 180 synthetic records
- `GET  /api/status` — whether data is seeded / a run has happened (drives empty state)
- `POST /api/run-agent` 🔒 — run the recovery agent over all cases
- `GET  /api/run-agent-stream` 🔒 — Server-Sent Events stream of per-case pipeline traces
- `POST /api/webhooks/razorpay` — real Razorpay webhook intake (authenticated by
  Razorpay's own signature, not the API key — see above)
- `POST /api/simulate` 🔒 — Policy Sandbox Monte Carlo simulation
- `POST /api/ask` — natural-language query → real filtered cases + summary
- `GET  /api/metrics` — core KPIs + naive baseline **and** dumb-persistence baseline
- `GET  /api/cases` — all cases with score, status, compliance, and source fields
- `GET  /api/cases/<id>/audit` — full audit trail + generated messages for one case
- `GET  /api/cases/<id>/explain` — per-case SHAP explanation (ML validation layer)
- `GET  /api/cohorts` — recovery rate by tenure bucket and merchant category
- `GET  /api/exceptions` — first-class list of unrecovered cases
- `GET  /api/rejected-webhooks` — events blocked at ingestion for bad signatures
- `GET  /api/activity` — recent audit_log events, newest first
- `GET  /api/messages/<id>` — Standard + Hinglish message variants
- `GET  /api/health/<id>` — per-customer subscription health score
- `GET  /api/audit-check` — correctness audit report (7 rules)
- `GET  /api/chaos-test` — adversarial chaos-suite report (7 scenarios)
- `GET  /api/ml-metrics`, `GET /api/ml-feature-importance` — ML validation layer
- `GET  /api/export` — CSV summary download
- `GET  /healthz` — liveness probe (no auth required)

## Performance & cost tradeoff (deliberate)

A full run over 180 cases stays well inside a live-demo budget. Locally the run completes
in ~2s cold and ~0.06s warm (LLM responses are cached by `case_id + type`, so re-runs
don't repeat calls).

Against a **real remote LLM**, narrating all 180 cases in one live run would add network
latency per call. To keep the demo snappy, live LLM narration is capped to the
**top-N highest-value cases** (default 20, via `LLM_LIVE_TOP_N`); the rest use the
deterministic templates. This is a conscious cost/latency decision — the highest-value
cases (the ones a judge is most likely to open) get the richest narration, and the cap
never affects any decision, number, or outcome. Set `LLM_LIVE_TOP_N=0` to narrate every
case with the LLM.

## Two baselines, not one (R11)

`baseline.py` runs **two** distinct baselines against the same seeded cases and the
same success-probability model as the real agent, so the comparison isolates exactly
what the agent's intelligence is worth:

1. **Naive baseline** — one attempt per case, no strategy at all.
2. **Dumb persistence baseline** — the *same* 3-attempt retry budget as the real
   agent, but still no scoring, no salary-window timing, no dunning, no
   promise-to-pay. This isolates "the value of trying more times" from "the value
   of the agent's actual strategy."

The dashboard shows all three bars, and the agent-vs-dumb-persistence delta is the
defensible number for what scoring/timing/dunning specifically contribute — not just
retry count. See `baseline.py`'s module docstring for the full rationale.

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

`backend/tests/` wraps the existing correctness/adversarial logic in real pytest
assertions (54 tests): the 7 `audit_check.py` business-rule checks re-run against a
full agent pass, the 7 `chaos_test.py` adversarial scenarios, unit tests for the
fail-closed webhook secret handling, the API-key gate, the Razorpay adapter
(signature verification + event mapping), the two baselines, and an end-to-end Flask
test-client test of `/api/webhooks/razorpay`. `.github/workflows/ci.yml` runs the
full suite plus the standalone `audit_check.py` / `chaos_test.py` CLI scripts on
every push.

You can still run the two standalone diagnostic scripts directly, or from the
dashboard's Compliance / Chaos Suite tabs:

```bash
python backend/audit_check.py
python backend/chaos_test.py
```

## Notes

- Deterministic: seeding and agent runs use a fixed RNG (`RUN_SEED = 42`) so results
  are reproducible — a full 180-case run always yields **139 recovered / 38 escalated
  / 3 rejected** (the 3 rejected cases are the deliberately spoofed webhooks that fail
  signature verification and never reach scoring). The four-agent refactor and the LLM
  budget preserve this exactly; `backend/tests/test_audit_invariants.py` pins these
  numbers so a future change that silently alters RNG order, retry cap, or scoring is
  caught immediately.
- Every dashboard number traces to real `audit_log` rows — nothing is hardcoded.
- The `audit_log` is append-only: the codebase only ever `INSERT`s audit rows (via
  `db.insert_audit`) — there is no `UPDATE` and no per-row `DELETE` code path anywhere.
  The single full-table wipe exists only in `reset_db()` for re-seeding a fresh demo.
- The 180-case simulation remains the primary demo data; real Razorpay webhooks are
  additive and flow through the identical pipeline (see above).

## Security hardening

- **Fail-closed webhook signatures, no insecure default.** Every seeded webhook
  carries an HMAC-SHA256 signature (`webhook_security.py`) over its canonical
  payload, keyed with `WEBHOOK_SECRET`. Unlike a typical demo shortcut, there is
  **no hardcoded fallback secret** — if `WEBHOOK_SECRET` is unset or equals a known
  placeholder value, every webhook is rejected rather than silently trusted against
  a publicly-known string. The pipeline verifies each event with
  `hmac.compare_digest` (constant-time) *before* processing; 3 events are
  deliberately seeded with a corrupted signature to simulate spoofed webhooks — they
  are logged as `webhook_rejected` and never enter the recovery pipeline (see the
  "Rejected webhooks" panel).
- **Separately fail-closed real Razorpay verification.** `razorpay_adapter.py` uses
  the same fail-closed pattern for `RAZORPAY_WEBHOOK_SECRET` against Razorpay's real
  signature scheme — a misconfigured deployment rejects every real webhook rather
  than silently accepting anything.
- **API-key gate on mutating endpoints.** `/api/reset`, `/api/seed`,
  `/api/run-agent`, `/api/run-agent-stream`, and `/api/simulate` require a valid
  `X-API-Key` header (`security.py`), checked in constant time. Without this, anyone
  who could reach the process could wipe/reseed the database or spend simulation
  compute with a single unauthenticated request. The dashboard's own JS fetches the
  current key once via a same-origin bootstrap call at page load, so normal use is
  unaffected. Read-only endpoints and the Razorpay webhook route (authenticated by
  Razorpay's own signature instead) are not gated by this key.
- **Locked-down NL query.** `/api/ask` validates the LLM's returned filter against a
  hardcoded field whitelist (`compliance_status`, `health_band`, `failure_reason`,
  `case_status`, `amount_min`, `amount_max`); off-list keys are dropped silently and
  an all-invalid result returns the graceful "try an example" message. Only whitelisted
  field names and parameterized values ever reach SQL — no raw LLM text is concatenated
  into a query.
- **PII masking.** `customer_id` is masked in the UI (first 4 + last 2, e.g.
  `CUST0042` → `CUST**42`); the real value stays in the database for lookups and joins.

This is a demo/hackathon security posture (a single shared API key, no user
accounts) — appropriate for this project's scope, not a production auth system.

