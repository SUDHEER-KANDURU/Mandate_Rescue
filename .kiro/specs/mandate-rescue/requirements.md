# Requirements — Mandate Rescue

## 1. Overview

Mandate Rescue is an AI-driven recovery system for failed UPI Autopay / e-mandate
auto-debits on a Razorpay-style merchant platform. Instead of sending one generic
reminder for every failure, the system diagnoses the failure type, scores how
recoverable each case is, applies a tailored recovery strategy, and keeps an honest,
fully-traceable audit trail — surfacing everything (including failures) on a clean
operations dashboard.

- **Stack:** Python Flask backend, SQLite database, vanilla HTML/CSS/JS dashboard.
- **Scope discipline:** Depth over breadth. No login systems, no unrelated admin
  panels, no features outside this document.

## 2. Glossary

- **Mandate failure:** A UPI Autopay auto-debit attempt that did not succeed.
- **Failure reason:** One of `insufficient_funds`, `mandate_expired`,
  `bank_technical_error`, `mandate_revoked`.
- **Recoverability score:** A weighted 0–100 score ranking how worth pursuing a case is.
- **Recovery strategy:** The reason-specific plan the agent applies to a case.
- **Escalation:** Handing a case to human/manual follow-up (terminal machine state).
- **Promise-to-pay (PTP):** Customer commits to pay by a date; may be kept or broken.
- **Exception:** Any case that ended without recovery (escalated, broken promise,
  revoked, or exhausted retries).

## 3. Data Requirements

### 3.1 `mandate_failures` table
Fields: `customer_id`, `amount`, `failure_reason`, `failure_date`,
`past_retry_count`, `customer_tenure_months`, `past_payment_success_rate`,
`merchant_category`, `case_status`.

### 3.2 `audit_log` table
Fields: `event_id`, `customer_id`, `event_timestamp`, `event_type`, `action_taken`,
`outcome`, `attempt_number`, `reasoning_text`, `case_status_after`.

### 3.3 Seed data
- Seed **180** synthetic records.
- Failure-reason mix: 45% `insufficient_funds`, 20% `mandate_expired`,
  20% `bank_technical_error`, 15% `mandate_revoked`.
- Amounts ₹199–₹15000, weighted toward the ₹500–₹3000 band.
- `failure_date` falls within the last 30 days.
- Include per-customer historical date/outcome hints so salary-window inference
  (Requirement 8) has something to learn from; otherwise fall back to generic windows.
## 4. Functional Requirements (EARS)

### R1 — Reason-specific recovery strategy
- WHEN a case has `failure_reason = insufficient_funds`, the system SHALL schedule a retry timed to the customer's salary window.
- WHEN a case has `failure_reason = mandate_expired`, the system SHALL generate a re-authorization link action instead of a blind retry.
- WHEN a case has `failure_reason = bank_technical_error`, the system SHALL perform a silent quick retry.
- WHEN a case has `failure_reason = mandate_revoked`, the system SHALL escalate immediately without retrying.

### R2 — Hard retry cap
- WHILE a case has had fewer than 3 retries, the system SHALL allow another retry when the strategy calls for it.
- WHEN a case reaches 3 retries without recovery, the system SHALL escalate the case and SHALL NOT retry further.

### R3 — Promise-to-pay sub-flow
- WHEN a promise-to-pay is recorded, the system SHALL set a promised-by date and status.
- WHEN the promised-by date passes without a successful payment, the system SHALL mark the promise broken and route the case down the broken-promise path (retry-if-budget or escalate).
- WHEN a promise is kept (payment succeeds by the date), the system SHALL mark the case recovered.

### R4 — Full, honest audit trail
- WHEN the agent takes any action on a case, the system SHALL write an `audit_log` row capturing event type, action taken, outcome, attempt number, plain-English reasoning, and resulting case status.
- The system SHALL log unsuccessful outcomes with the same fidelity as successful ones.
- The system SHALL expose the full per-case audit trail in the UI, including failures.

### R5 — Core dashboard metrics
- The system SHALL display ₹ at risk vs ₹ recovered, recovery rate, and escalation rate.
- The system SHALL display a first-class Exceptions list.
- Every metric SHALL be computed from real `audit_log` rows — no hardcoded or decorative numbers.

### R6 — Recoverability score (triage, not just rules)
- The system SHALL compute a weighted 0–100 recoverability score per case from `past_payment_success_rate`, `customer_tenure_months`, `past_retry_count`, and `failure_reason`.
- The system SHALL display the score in the UI and SHALL process/prioritize higher-scoring cases first.

### R7 — Explainability panel
- WHEN the agent makes a decision, the system SHALL show a plain-English "why" citing the concrete factors (e.g. historical success rate, attempt number, salary window).
- The reasoning text SHALL match what is stored in `audit_log.reasoning_text`.

### R8 — Personalized salary-window learning (v2)
- WHERE a customer has enough historical date/outcome data, the system SHALL infer a per-customer salary window.
- WHERE history is insufficient, the system SHALL fall back to generic salary windows.
- The UI SHALL clearly label inferred windows as "v2 personalization" versus generic fallback.

### R9 — Channel + tone message generation
- WHEN a nudge is sent, the system SHALL generate the actual message text for the case (not just "SMS sent").
- The system SHALL offer at least one Hinglish-tone variant alongside a standard variant, and record the channel/tone chosen in the audit trail.

### R10 — Cohort / segment view
- The system SHALL show recovery rate broken down by `customer_tenure_months` bucket and by `merchant_category`.

### R11 — Agent vs baseline comparison
- The system SHALL compute a naive baseline (retry everyone once, same generic message) and display its recovered ₹ next to the agent's actual recovered ₹.
- The baseline SHALL be derived from the same seeded cases so the comparison is apples-to-apples.

### R12 — Exportable report
- The system SHALL provide a "Download Summary" action exporting key dashboard metrics and the exceptions list as a clean CSV (PDF optional).

## 4b. Razorpay Realism & Compliance Requirements

These requirements ground the simulation in Razorpay's real UPI Autopay / Subscriptions
product behavior. R13–R16 are in scope for the main build; R17 is stretch/optional.

### R13 — Webhook-shaped ingestion
- Incoming failures SHALL be modeled as if delivered via Razorpay-style webhook payloads.
- Each case SHALL store a `raw_event_type` matching a real event name — one of `subscription.charged.failed`, `subscription.halted`, `payment.failed` — instead of a generic "failure" label.
- The system SHALL surface the triggering event in the audit trail and case detail view (e.g. "Triggered by: subscription.charged.failed webhook").

### R14 — UPI mandate limit awareness
- Each case SHALL carry a `mandate_limit` field (default ₹5000), with some seeded cases above the limit to create realistic edge cases.
- WHEN `amount > mandate_limit`, the system SHALL NOT attempt a normal retry; instead it SHALL flag the case as "requires mandate re-authorization at higher limit" and route it like `mandate_expired`.
- The UI and `reasoning_text` SHALL clearly distinguish this compliance edge case from an ordinary retry.

### R15 — RBI pre-debit notification compliance
- WHEN the system schedules any retry, it SHALL log a pre-debit notification event timestamped at least 24 hours before the retry attempt.
- The system SHALL mark each case `RBI-compliant` or `non-compliant` based on whether that 24-hour gap was honored.
- The UI SHALL show a compliance badge per case, supporting the brief's "compliant escalation" requirement.

### R16 — Staged dunning communication
- The system SHALL replace ad hoc single nudges with a 3-stage dunning sequence per Razorpay's dunning model: Day 1 friendly reminder → Day 3 firmer follow-up → Day 7 final notice before escalation.
- Each stage SHALL have a distinct message tone and SHALL be logged as a separate audit event.
- The UI SHALL show dunning-stage progress per case (e.g. a 3-step progress indicator).

### R17 — Subscription health score (STRETCH / OPTIONAL)
- In addition to the per-case recoverability score (R6), the system MAY compute a lightweight per-customer "health" indicator combining `past_payment_success_rate` and `past_retry_count` across all of a customer's cases to signal churn risk.
- This reflects Razorpay's positioning of its Subscriptions dashboard as a single control hub for overall subscription health.
- R17 SHALL NOT block or destabilize the core demo; it is built only if time permits after R13–R16.

## 5. Non-Functional / Quality Requirements

- **N1 Traceability:** Every recovered/escalated number on the dashboard MUST map to real `audit_log` rows. No hardcoded figures anywhere.
- **N2 Honesty of exceptions:** The Exceptions panel MUST be a first-class section — never hidden, minimized, or styled as a footnote.
- **N3 Clean UI:** Cards, one accent color, clear typography, minimal visual noise. Professional, not a spreadsheet dump.
- **N4 Determinism for demo:** Seeding and agent runs SHALL use a fixed random seed so results are reproducible during evaluation.
- **N5 Scope discipline:** No login/auth, no unrelated admin panels, no out-of-scope features.
- **N6 Local-first:** Runs locally with `python app.py` against a single SQLite file; no external services required.

## 6. Out of Scope
- Real Razorpay API integration (simulated only).
- Real SMS/WhatsApp/email delivery (message text is generated and logged, not sent).
- User authentication, roles, or multi-tenant admin.
- Heavy frontend frameworks (React/Vue/etc.).

## 7. Acceptance Criteria Summary
- 180 seeded records with the specified distributions exist in SQLite.
- Running the agent produces `audit_log` rows for every action, including failures.
- Dashboard shows R5 metrics, all traceable to `audit_log`.
- Score (R6), explainability (R7), salary-window learning (R8), message generation (R9), cohort view (R10), baseline comparison (R11), and export (R12) are all present and functional.
- Retry cap (max 3 → escalate) and promise-to-pay broken path are demonstrably enforced.
- Webhook-shaped ingestion (R13), mandate-limit routing (R14), RBI pre-debit compliance badges (R15), and 3-stage dunning (R16) are present and functional.
- R17 (subscription health score) is optional and does not block the core demo.
