# Design — Mandate Rescue

## 1. Architecture Overview

A single Flask app serving a JSON API and a static dashboard, backed by one SQLite
file. A deterministic simulation engine ("the agent") walks each seeded case through a
strategy state machine, writing an audit row for every action.

```
+---------------------+        +--------------------------+
|  Browser dashboard  |  HTTP  |        Flask app         |
|  (HTML/CSS/JS)      | <----> |  /api/* JSON endpoints   |
+---------------------+        +------------+-------------+
                                            |
                    +-----------------------+-----------------------+
                    |            |            |            |         |
                 seed.py    agent.py     scoring.py   messaging.py  db.py
                (synthetic  (state      (recover-    (channel/tone (SQLite
                 records)    machine)    ability)     text gen)     access)
                                            |
                                     mandate_rescue.db
                                  (mandate_failures, audit_log)
```

- **Language/stack:** Python 3, Flask, sqlite3 (stdlib), vanilla JS + Chart.js (CDN) for charts.
- **No ORM:** thin `db.py` with parameterized SQL to keep it transparent and auditable.

## 2. Project Structure

```
Mandate_Rescue/
├── app.py                 # Flask entrypoint + API routes
├── db.py                  # SQLite connection + schema init helpers
├── seed.py                # synthetic 180-record generator (fixed seed)
├── scoring.py             # recoverability score
├── salary_window.py       # per-customer window inference + fallback
├── messaging.py           # channel/tone message generation (incl. Hinglish)
├── agent.py               # recovery state machine + audit logging
├── metrics.py             # dashboard aggregations from audit_log
├── baseline.py            # naive baseline simulation
├── export.py              # CSV summary export
├── requirements.txt
├── mandate_rescue.db      # generated at runtime (gitignored)
├── static/
│   ├── style.css
│   └── app.js
└── templates/
    └── index.html
```

## 3. Data Model (SQLite DDL)

`mandate_failures`:
- `customer_id` TEXT PRIMARY KEY
- `amount` REAL — ₹199–15000
- `failure_reason` TEXT — insufficient_funds | mandate_expired | bank_technical_error | mandate_revoked
- `failure_date` TEXT — ISO date within last 30 days
- `past_retry_count` INTEGER
- `customer_tenure_months` INTEGER
- `past_payment_success_rate` REAL — 0.0–1.0
- `merchant_category` TEXT — subscription | emi | insurance | utility
- `case_status` TEXT — new | in_progress | promised | recovered | escalated | broken_promise
- `raw_event_type` TEXT — Razorpay webhook event: subscription.charged.failed | subscription.halted | payment.failed (R13)
- `mandate_limit` REAL — UPI mandate cap, default 5000; some cases seeded above it (R14)
- `compliance_status` TEXT — RBI-compliant | non-compliant, set once a retry is scheduled (R15)
- `dunning_stage` INTEGER — current dunning stage 0–3 (0 = none, 1/2/3 = Day1/Day3/Day7) (R16)
- `health_score` REAL — optional per-customer churn-risk indicator (R17, stretch)

`audit_log`:
- `event_id` INTEGER PRIMARY KEY AUTOINCREMENT
- `customer_id` TEXT
- `event_timestamp` TEXT — ISO datetime
- `event_type` TEXT — webhook_received | score | strategy_selected | retry | reauth_link | silent_retry | mandate_limit_block | pre_debit_notification | dunning_stage | escalate | promise_recorded | promise_kept | promise_broken | nudge_sent
- `action_taken` TEXT — human-readable action
- `outcome` TEXT — success | failure | pending | n/a
- `attempt_number` INTEGER — 0 for non-retry events
- `reasoning_text` TEXT — plain-English "why" (feeds R7 explainability panel)
- `case_status_after` TEXT — case_status value after this event

Indexes on `audit_log(customer_id)` and `audit_log(event_type)` for fast dashboard aggregation.

**Realism/compliance fields (R13–R16):** `raw_event_type` is seeded from `failure_reason` (e.g. `mandate_revoked` → `subscription.halted`, others → `subscription.charged.failed` / `payment.failed`) and echoed in the audit trail as "Triggered by: <event> webhook". `mandate_limit` defaults to ₹5000 with a minority of cases seeded above it. `compliance_status` is derived from the ≥24h pre-debit gap. `dunning_stage` advances as staged nudges are sent.

## 4. Recoverability Score (scoring.py)

A transparent weighted 0–100 score. Higher = more worth pursuing.

Normalize each input to 0–1, then combine:
- `success_component` = `past_payment_success_rate` (weight 0.40)
- `tenure_component` = min(`customer_tenure_months` / 24, 1.0) (weight 0.20)
- `retry_component` = 1 − min(`past_retry_count` / 3, 1.0) (weight 0.20) — fewer prior retries is better
- `reason_component` = per-reason base recoverability (weight 0.20):
  - bank_technical_error → 0.95 (usually transient)
  - insufficient_funds → 0.70 (recoverable with timing)
  - mandate_expired → 0.55 (needs customer re-auth action)
  - mandate_revoked → 0.10 (rarely recoverable)

`score = round(100 * (0.40*success + 0.20*tenure + 0.20*retry + 0.20*reason))`

Weights and the reason table live as named constants so they're easy to audit and tune.
The score is logged as a `score` event with reasoning text, so it is traceable.

## 5. Salary-Window Learning (salary_window.py)

- **Generic fallback windows:** salary typically credited around month-end / 1st and mid-month; generic retry windows are days 1–3 and 25–31 (labelled "generic").
- **v2 personalization:** if a customer's history hints (embedded in seed as prior successful-attempt day-of-month values) show enough data points (≥3), infer the modal successful day-of-month band and schedule the retry there. Label as "inferred (v2 personalization)".
- The chosen window and whether it was inferred vs generic is written into `reasoning_text` and surfaced in the UI badge.

## 6. Message Generation (messaging.py)

- Generates real message text per case, templated by `failure_reason` and `merchant_category`, personalized with amount and (masked) customer id.
- Two tone variants per message: **Standard** (polite English) and **Hinglish** (casual English-Hindi mix). Example intent — a friendly reminder that the auto-debit didn't go through and how to fix it.
- Channel options: SMS / WhatsApp / Email (simulated — text is generated and logged via a `nudge_sent` audit event, never actually delivered).
- No PII beyond synthetic seed data; customer ids are synthetic.

## 7. Recovery Agent State Machine (agent.py)

Cases are processed in descending recoverability-score order (R6 triage). Each case starts at `new`, and every transition writes an audit row.

Case statuses: `new`, `in_progress`, `promised`, `recovered`, `escalated`, `broken_promise`.

Per-case pre-processing (before strategy selection):
- **Webhook logging (R13):** log a `webhook_received` event capturing `raw_event_type` ("Triggered by: <event> webhook").
- **Mandate-limit gate (R14):** if `amount > mandate_limit`, log a `mandate_limit_block` event, flag "requires mandate re-authorization at higher limit", and route the case down the `mandate_expired` (re-auth) path instead of a normal retry — regardless of the original `failure_reason`.

Per-reason flow:
- **insufficient_funds:** score → log strategy → schedule salary-window retry → simulate outcome. On success → `recovered`. On failure → increment attempt; if attempts < 3 retry at next window, else escalate. May branch into promise-to-pay.
- **mandate_expired:** score → log strategy → generate re-auth link + nudge. If customer "re-auths" (simulated by success probability) → `recovered`; else escalate after cap.
- **bank_technical_error:** score → silent quick retry (short delay). High success probability; on repeated failure within cap → escalate.
- **mandate_revoked:** score → immediate `escalated` (no retry), with reasoning noting revocation.

Hard rules enforced centrally:
- **Retry cap (R2):** a guard checks attempt count before any retry; the 3rd unrecovered attempt forces `escalated`. No code path can exceed 3 retries.
- **Promise-to-pay (R3):** during a nudge, a case may record a promise (`promised`, promised-by date). A scheduled check marks it `recovered` if paid by date, else `broken_promise` → broken-promise path (one more retry if within cap, else escalate).
- **RBI pre-debit compliance (R15):** before any retry is attempted, log a `pre_debit_notification` event timestamped ≥24h before the retry. Set `compliance_status` to `RBI-compliant` when the 24h gap is honored, else `non-compliant`. The scheduler always aims for compliance; `non-compliant` only occurs in deliberately-seeded edge cases so the badge is meaningful.
- **Staged dunning (R16):** nudges follow a 3-stage sequence — Day 1 friendly → Day 3 firmer → Day 7 final notice before escalation. Each stage logs a distinct `dunning_stage` audit event with its own tone, and advances `dunning_stage` (1→2→3). Escalation follows the Day 7 final notice if still unrecovered (subject to the retry cap).

Outcome simulation:
- Each attempt's success is drawn from a probability derived from failure_reason base rate, recoverability score, and salary-window fit — seeded RNG for reproducibility (N4). This keeps recovered numbers realistic and non-hardcoded; the actual outcome is what gets logged and counted.

## 8. Metrics (metrics.py) — all derived from audit_log

- **₹ at risk:** sum of `amount` across all seeded cases.
- **₹ recovered:** sum of `amount` for cases whose latest audit status is `recovered`.
- **Recovery rate:** recovered cases / total cases (and ₹-weighted variant).
- **Escalation rate:** escalated cases / total cases.
- **Exceptions:** cases ending in `escalated`, `broken_promise`, or `revoked`-driven escalation, with reason and last action.
- All figures are computed by querying `audit_log` (and the final `case_status`), never stored as constants (N1).

## 9. Baseline Comparison (baseline.py)

- Simulates the naive approach: one retry per case with a single generic message, no scoring, no timing, no promise flow.
- Uses the same seeded cases and same RNG discipline so it's comparable.
- Returns baseline ₹ recovered and recovery rate for side-by-side display (R11).

## 10. API Endpoints (app.py)

- `POST /api/seed` — (re)generate the 180 synthetic records (fixed seed).
- `POST /api/run-agent` — run the agent over all cases; returns run summary.
- `GET  /api/metrics` — core dashboard metrics (R5) + baseline (R11).
- `GET  /api/cases` — case list with score, status, and reason (sortable by score).
- `GET  /api/cases/<customer_id>/audit` — full audit trail for one case (R4/R7).
- `GET  /api/cohorts` — recovery rate by tenure bucket and merchant_category (R10).
- `GET  /api/exceptions` — first-class exceptions list (R5/N2).
- `GET  /api/messages/<customer_id>` — generated message variants incl. Hinglish (R9).
- `GET  /api/export` — CSV summary download (R12).
- `GET  /api/health/<customer_id>` — (stretch) per-customer subscription health score (R17).

The `/api/cases` and `/api/cases/<id>/audit` payloads SHALL include `raw_event_type` (R13), `mandate_limit` + mandate-limit-block flag (R14), `compliance_status` (R15), and `dunning_stage` (R16) so the UI can render the webhook trigger, compliance badge, and dunning progress.

All endpoints return JSON except `/api/export` (text/csv).

## 11. Frontend / Dashboard (templates + static)

Layout — single page, card-based, one accent color, clear typography (N3):
- **Header + actions:** title, "Seed data", "Run agent", "Download summary" buttons.
- **KPI cards row:** ₹ at risk, ₹ recovered, recovery rate, escalation rate.
- **Agent vs Baseline card:** two-bar comparison (Chart.js) of ₹ recovered (R11).
- **Cases table:** sortable by recoverability score; columns for score, customer, reason, amount, status, salary-window badge (generic vs inferred v2).
- **Case detail drawer:** on row click — full audit trail (R4), explainability text (R7), and generated message variants with tone/channel toggle incl. Hinglish (R9).
- **Cohort view:** recovery-rate breakdown by tenure bucket and merchant_category (R10).
- **Exceptions section:** first-class, always visible, never minimized or footnoted (N2) — each row shows customer, reason, last action, and why it wasn't recovered.

## 12. Design Decisions & Trade-offs

- **No ORM / thin SQL:** keeps data access transparent and easy to audit; matches N1.
- **Deterministic RNG (seeded):** reproducible demos (N4) while keeping recovered numbers emergent from simulation rather than hardcoded (N1).
- **Chart.js via CDN only:** minimal frontend footprint, no build step, satisfies the "no heavy framework" constraint.
- **Score as guidance, rules as guardrails:** the score prioritizes work order (R6) but the hard retry cap and revoked-immediate-escalation rules always override (R1/R2), so triage never breaks safety rules.
- **Messages generated, not sent:** avoids real delivery integrations and any PII/compliance risk while still proving message-generation depth (R9).

## 13. Traceability Matrix

- R1 → agent.py per-reason flow
- R2 → agent.py retry-cap guard
- R3 → agent.py promise-to-pay branch
- R4 → audit_log writes on every transition + `/api/cases/<id>/audit`
- R5 → metrics.py + KPI cards + exceptions
- R6 → scoring.py + score column + triage order
- R7 → reasoning_text + explainability panel
- R8 → salary_window.py + UI badge
- R9 → messaging.py + message drawer
- R10 → `/api/cohorts` + cohort view
- R11 → baseline.py + comparison card
- R12 → export.py + `/api/export`
- R13 → `raw_event_type` field + `webhook_received` event + "Triggered by" in audit/case detail
- R14 → `mandate_limit` field + `mandate_limit_block` gate in agent.py + UI note
- R15 → `pre_debit_notification` event + `compliance_status` + UI compliance badge
- R16 → 3-stage `dunning_stage` events + UI progress indicator
- R17 → (stretch) `health_score` + `/api/health/<id>` + dashboard health indicator
