# Tasks — Mandate Rescue

Implementation plan. Each task is incremental, buildable, and traces back to
requirements (R#) and design sections. Do not start until the spec is approved.

- [ ] 1. Project scaffold and dependencies
  - Create folder structure per design §2 (`app.py`, `db.py`, `static/`, `templates/`, etc.).
  - Add `requirements.txt` (Flask) and a `.gitignore` excluding `mandate_rescue.db`.
  - _Requirements: N5, N6_

- [ ] 2. Database layer and schema (`db.py`)
  - Implement SQLite connection helper and schema init for `mandate_failures` and `audit_log` per design §3, with indexes.
  - Provide small parameterized query helpers (no ORM).
  - _Requirements: R4, N1, N6_

- [ ] 3. Synthetic seed generator (`seed.py`)
  - Generate 180 records with the required reason mix (45/20/20/15), amounts ₹199–15000 weighted to ₹500–3000, failure_date within last 30 days.
  - Embed per-customer historical success-day hints for salary-window inference.
  - Use a fixed random seed for reproducibility.
  - _Requirements: 3.3, R8, N4_

- [ ] 4. Recoverability scoring (`scoring.py`)
  - Implement the weighted 0–100 score per design §4 with named constants for weights and the per-reason table.
  - Return both the score and a short factor breakdown for reasoning text.
  - _Requirements: R6, R7_

- [ ] 5. Salary-window inference (`salary_window.py`)
  - Implement generic fallback windows and per-customer inference (≥3 data points) per design §5.
  - Return the chosen window plus an `inferred` vs `generic` flag for the UI badge.
  - _Requirements: R8_

- [ ] 6. Message generation (`messaging.py`)
  - Generate real message text per failure_reason and merchant_category.
  - Provide Standard and Hinglish tone variants and SMS/WhatsApp/Email channel options.
  - _Requirements: R9_

- [ ] 7. Recovery agent state machine (`agent.py`)
  - [ ] 7.1 Core loop: process cases in descending score order; log a `score` and `strategy_selected` event per case.
  - [ ] 7.2 Per-reason strategies (salary-window retry, re-auth link, silent retry, immediate escalate) per design §7 and R1.
  - [ ] 7.3 Retry-cap guard: max 3 retries then mandatory escalation (R2).
  - [ ] 7.4 Promise-to-pay branch with kept / broken paths (R3).
  - [ ] 7.5 Seeded outcome simulation; write an `audit_log` row for every transition incl. failures (R4, N1, N4).
  - _Requirements: R1, R2, R3, R4, R6, N4_
- [ ] 8. Metrics aggregation (`metrics.py`)
  - Compute ₹ at risk, ₹ recovered, recovery rate, escalation rate, and exceptions — all from `audit_log` / final case status (no hardcoded numbers).
  - _Requirements: R5, N1, N2_

- [ ] 9. Baseline simulation (`baseline.py`)
  - Simulate naive "retry once, generic message" over the same seeded cases; return baseline ₹ recovered and rate.
  - _Requirements: R11, N4_

- [ ] 10. Webhook-shaped ingestion (`seed.py` + `db.py`)
  - Add `raw_event_type` to `mandate_failures`; populate with Razorpay-style event names (`subscription.charged.failed`, `subscription.halted`, `payment.failed`) mapped from `failure_reason`.
  - Log the triggering event in the audit trail ("Triggered by: <event> webhook").
  - _Requirements: R13_

- [ ] 11. UPI mandate limit awareness (`db.py` + `agent.py`)
  - Add `mandate_limit` (default ₹5000; seed some cases above it).
  - WHEN `amount > mandate_limit`, skip normal retry, flag "requires mandate re-authorization at higher limit", and route like `mandate_expired` with clear reasoning text.
  - _Requirements: R14_

- [ ] 12. RBI pre-debit notification compliance (`agent.py`)
  - WHEN scheduling any retry, log a `pre_debit_notification` event timestamped ≥24h before the retry attempt.
  - Mark each case `RBI-compliant` or `non-compliant` based on whether the 24h gap was honored; expose for the UI badge.
  - _Requirements: R15_

- [ ] 13. Staged dunning communication (`messaging.py` + `agent.py`)
  - Implement a 3-stage dunning sequence: Day 1 friendly → Day 3 firmer → Day 7 final notice before escalation, each with distinct tone.
  - Log each stage as a separate audit event and track current dunning stage per case for the UI progress indicator.
  - _Requirements: R16, R9_

- [ ] 14. Flask API endpoints (`app.py`)
  - Implement all endpoints per design §10, including `raw_event_type`, `mandate_limit`, compliance status, and dunning stage in the case/audit payloads.
  - _Requirements: R4, R5, R9, R10, R11, R12, R13, R14, R15, R16_

- [ ] 15. Dashboard frontend (`templates/index.html`, `static/style.css`, `static/app.js`)
  - [ ] 15.1 KPI cards, agent-vs-baseline chart, and sortable cases table (score column).
  - [ ] 15.2 Case detail drawer: audit trail with "Triggered by: <webhook>", explainability text, message variants (incl. Hinglish), mandate-limit note, RBI compliance badge, and 3-step dunning progress indicator.
  - [ ] 15.3 Cohort view (tenure bucket + merchant_category).
  - [ ] 15.4 First-class Exceptions section (never minimized).
  - _Requirements: R5, R6, R7, R8, R9, R10, R11, N2, N3, R13, R14, R15, R16_

- [ ] 16. Exportable report (`export.py` + `/api/export`)
  - Export key metrics and the exceptions list as clean CSV via a "Download Summary" button.
  - _Requirements: R12_

- [ ] 17. Verification pass
  - Confirm 180 seeded records with correct distributions; run agent; verify every dashboard number traces to `audit_log` rows.
  - Verify retry cap, promise-to-pay broken path, mandate-limit routing, RBI compliance badges, and dunning stages behave as specified.
  - _Requirements: all core (R1–R16, N1–N6)_

- [ ] 18. (STRETCH / OPTIONAL) Subscription health score (R17)
  - Only after R13–R16 are complete and working. Compute a lightweight per-customer "health" indicator from `past_payment_success_rate` and `past_retry_count` across their cases to signal churn risk; surface on the dashboard.
  - Must not block or destabilize the core demo if time runs short.
  - _Requirements: R17_

---

## Pre-Task-10 Clarifications (binding — read before starting task 10 onward)

### C1 — Task 10 webhook mapping (exact, deterministic — do not guess)
Populate `raw_event_type` from `failure_reason` using this exact mapping:

- `insufficient_funds`   → `subscription.charged.failed`
- `bank_technical_error` → `subscription.charged.failed`
- `mandate_expired`      → `payment.failed`
- `mandate_revoked`      → `subscription.halted`
