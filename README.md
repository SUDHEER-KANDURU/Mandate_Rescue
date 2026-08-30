# Mandate Rescue

AI-driven recovery for failed UPI Autopay / e-mandate auto-debits on a Razorpay-style
merchant platform. Instead of one generic reminder per failure, the agent diagnoses the
failure type, scores how recoverable each case is, applies a tailored strategy, and keeps
an honest, fully-traceable audit trail — all surfaced on a clean operations dashboard.

The recovery flow runs as an explicit **four-agent pipeline**
(Diagnosis → Triage → Strategy → Communication). See [ARCHITECTURE.md](ARCHITECTURE.md)
for the diagram and stage-by-stage responsibilities.

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
│   ├── baseline.py          # naive baseline simulation (R11)
│   ├── export.py            # CSV summary export (R12)
│   └── health.py            # subscription health score (R17)
├── frontend/                # Dashboard UI (vanilla HTML/CSS/JS)
│   ├── templates/index.html
│   └── static/{style.css, app.js}
├── ARCHITECTURE.md          # four-agent pipeline diagram (Mermaid)
├── mandate_rescue.db        # generated at runtime (gitignored)
├── requirements.txt
└── README.md
```

## Setup & run

```bash
pip install -r requirements.txt
python backend/app.py
```

Then open http://127.0.0.1:5000. Use **Reset demo** to seed fresh data, then **Run agent**
to watch cases stream through the pipeline live.

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

- `POST /api/reset` — re-seed fresh data + clear the previous run and LLM cache
- `POST /api/seed` — (re)generate the 180 synthetic records
- `GET  /api/status` — whether data is seeded / a run has happened (drives empty state)
- `POST /api/run-agent` — run the recovery agent over all cases
- `GET  /api/run-agent-stream` — Server-Sent Events stream of per-case pipeline traces
- `POST /api/ask` — natural-language query → real filtered cases + summary
- `GET  /api/metrics` — core KPIs + naive baseline comparison
- `GET  /api/cases` — all cases with score, status, and compliance fields
- `GET  /api/cases/<id>/audit` — full audit trail + generated messages for one case
- `GET  /api/cohorts` — recovery rate by tenure bucket and merchant category
- `GET  /api/exceptions` — first-class list of unrecovered cases
- `GET  /api/messages/<id>` — Standard + Hinglish message variants
- `GET  /api/health/<id>` — per-customer subscription health score
- `GET  /api/export` — CSV summary download

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

## Notes

- Simulated only: no real Razorpay API, SMS/WhatsApp/email delivery, or auth.
- Deterministic: seeding and agent runs use a fixed RNG (`RUN_SEED = 42`) so results are
  reproducible — a full run always yields **142 recovered / 38 escalated**. The
  four-agent refactor and the LLM budget preserve this exactly.
- Every dashboard number traces to real `audit_log` rows — nothing is hardcoded.
- The `audit_log` is append-only: the codebase only ever `INSERT`s audit rows (via
  `db.insert_audit`) — there is no `UPDATE` and no per-row `DELETE` code path anywhere.
  The single full-table wipe exists only in `reset_db()` for re-seeding a fresh demo.

## Security hardening

- **Webhook signature verification.** Every seeded webhook carries an HMAC-SHA256
  signature (`webhook_security.py`) over its canonical payload, keyed with
  `WEBHOOK_SECRET`. The pipeline verifies each event with `hmac.compare_digest`
  (constant-time) *before* processing; 3 events are deliberately seeded with a
  corrupted signature to simulate spoofed webhooks — they are logged as
  `webhook_rejected` and never enter the recovery pipeline (see the "Rejected
  webhooks" panel). Set `WEBHOOK_SECRET` in `.env` (see `.env.example`).
- **Locked-down NL query.** `/api/ask` validates the LLM's returned filter against a
  hardcoded field whitelist (`compliance_status`, `health_band`, `failure_reason`,
  `case_status`, `amount_min`, `amount_max`); off-list keys are dropped silently and
  an all-invalid result returns the graceful "try an example" message. Only whitelisted
  field names and parameterized values ever reach SQL — no raw LLM text is concatenated
  into a query.
- **PII masking.** `customer_id` is masked in the UI (first 4 + last 2, e.g.
  `CUST0042` → `CUST**42`); the real value stays in the database for lookups and joins.

