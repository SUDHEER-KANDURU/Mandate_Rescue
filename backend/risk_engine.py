"""Revenue-at-Risk Prediction Engine — Phase 5.

Predicts which ACTIVE (case_status='new' or 'in_progress') subscriptions are at
elevated risk of failure BEFORE or DURING the recovery window, so the merchant can
intervene early rather than react after failure.

Design principles
-----------------
- All scores are derived from real stored features — no hardcoded outcomes.
- Every risk score output carries a ``data_type`` key: "actual" | "estimate".
- Risk scores are expressed as 0–100 (higher = more at risk / more urgent to act).
- Contributing factors are always returned so the UI can answer "why is this risky?"
- Intervention windows are derived from salary_window logic where applicable.
- We do NOT predict future payment failures from outside data; we score the
  difficulty of recovering if a failure DOES occur, which is directly measurable
  from the existing features and outcome history.

Risk scoring methodology
------------------------
The risk score for a case is the complement of the recoverability score, weighted
by the financial exposure:

  urgency_score   = 100 - recoverability_score     (harder to recover → more urgent)
  exposure_weight = amount / p95_amount             (bigger amounts matter more)
  risk_score      = clamp(urgency_score × (0.6 + 0.4 × exposure_weight), 0, 100)

Contributing factors are taken directly from the scoring.score_case() factors dict
and augmented with:
  - failure_reason severity (mandate_revoked is highest, bank_technical_error lowest)
  - health_band (high-risk health → higher risk flag)
  - past_retry_count (exhausted retries → harder to recover)
  - over_limit flag (blocks standard retry path)

Intervention window
-------------------
For insufficient_funds cases, the optimal retry window comes from salary_window.py.
For other reasons, a generic 24-48 hour window is returned.
"""

import math
from typing import Optional

import db
import scoring
import salary_window as sw_module
import health as health_module

# Failure reason severity weights (used in factor explanation, not in the score directly)
_REASON_SEVERITY = {
    "mandate_revoked": "critical",      # policy: no retry, immediate escalation
    "mandate_expired": "high",          # requires customer re-auth
    "insufficient_funds": "medium",     # timing-sensitive, recoverable
    "bank_technical_error": "low",      # usually transient, high auto-recovery
}

# Health band risk modifier
_HEALTH_RISK_MOD = {
    "high-risk": 15,    # additional urgency points
    "at-risk":    5,
    "healthy":    0,
}

# Amount percentile cache (computed once per call batch)
def _p95_amount(cases: list) -> float:
    amounts = sorted(float(c["amount"]) for c in cases)
    if not amounts:
        return 1.0
    idx = int(math.ceil(0.95 * len(amounts))) - 1
    return max(amounts[min(idx, len(amounts)-1)], 1.0)


def score_case_risk(case: dict, p95_amount: float = None) -> dict:
    """Compute a risk score + contributing factors for one case.

    Returns a dict with:
        customer_id, amount, failure_reason, case_status,
        risk_score (0–100, higher = more urgent to act),
        recoverability_score (0–100, from scoring.py),
        contributing_factors (list of {factor, value, impact}),
        severity (critical/high/medium/low),
        intervention_window (dict from salary_window or generic),
        data_type ("estimate" — derived from features, not observed outcome)
    """
    score, factors = scoring.score_case(case)
    h_score = health_module.health_score(
        case.get("past_payment_success_rate", 0.0),
        case.get("past_retry_count", 0),
    )
    h_band = health_module.health_band(h_score)
    reason = case.get("failure_reason", "")
    amount = float(case.get("amount", 0) or 0)

    # Base urgency: harder to recover → more urgent to intervene.
    urgency = 100 - score  # score=97 → urgency=3 (easy case); score=10 → urgency=90

    # Exposure weight: normalise amount to p95 so high-value cases bubble up.
    if p95_amount is None:
        p95_amount = max(amount, 1.0)
    exp_weight = min(amount / p95_amount, 1.0)
    risk_score = urgency * (0.6 + 0.4 * exp_weight)

    # Health band modifier
    health_mod = _HEALTH_RISK_MOD.get(h_band, 0)
    risk_score = min(100, risk_score + health_mod)

    # Over-limit block adds urgency (standard retry path blocked)
    over_limit = amount > float(case.get("mandate_limit") or 5000)
    if over_limit:
        risk_score = min(100, risk_score + 8)

    risk_score = round(risk_score)

    # Build contributing factors list for the UI
    contributing_factors = [
        {
            "factor": "Recoverability score",
            "value": f"{score}/100",
            "impact": "lower score → harder to recover → higher risk",
        },
        {
            "factor": "Failure reason",
            "value": reason,
            "impact": f"severity: {_REASON_SEVERITY.get(reason, 'unknown')}",
        },
        {
            "factor": "Subscription health",
            "value": f"{h_band} ({h_score}/100)",
            "impact": f"+{health_mod} urgency points" if health_mod else "no additional urgency",
        },
        {
            "factor": "Historical success rate",
            "value": f"{int(factors.get('success_rate', 0)*100)}%",
            "impact": "below 50% raises recovery difficulty",
        },
        {
            "factor": "Past retry count",
            "value": str(case.get("past_retry_count", 0)),
            "impact": "more prior retries exhaust the recovery budget sooner",
        },
    ]
    if over_limit:
        contributing_factors.append({
            "factor": "Over mandate limit",
            "value": f"Rs {amount:,.0f} > limit Rs {float(case.get('mandate_limit') or 5000):,.0f}",
            "impact": "+8 urgency — standard retry path blocked, re-auth required",
        })

    # Intervention window
    if reason == "insufficient_funds":
        window = sw_module.infer_window(case)
        intervention = {
            "type": "salary_window",
            "label": window["label"],
            "window_days": window.get("window"),
            "rationale": window.get("reason", ""),
        }
    elif reason == "mandate_revoked":
        intervention = {
            "type": "none",
            "label": "No retry — immediate escalation required",
            "rationale": "Mandate revoked: customer must re-consent.",
        }
    else:
        intervention = {
            "type": "immediate",
            "label": "Retry within 24–48 hours",
            "rationale": (
                "bank_technical_error: transient, retry quickly."
                if reason == "bank_technical_error"
                else "mandate_expired: send re-auth link immediately."
            ),
        }

    return {
        "customer_id": case["customer_id"],
        "amount": amount,
        "failure_reason": reason,
        "merchant_category": case.get("merchant_category", ""),
        "case_status": case.get("case_status", ""),
        "risk_score": risk_score,
        "recoverability_score": score,
        "health_score": h_score,
        "health_band": h_band,
        "severity": _REASON_SEVERITY.get(reason, "medium"),
        "over_limit": over_limit,
        "contributing_factors": contributing_factors,
        "intervention_window": intervention,
        "data_type": "estimate",
        "data_type_note": (
            "Risk score is derived from stored case features via the recoverability "
            "model. It is an estimate, not a guaranteed outcome."
        ),
    }


def revenue_at_risk(conn, include_recovered: bool = False) -> dict:
    """Score all active cases and return a prioritised at-risk list.

    Args:
        include_recovered: if True include already-recovered cases (for analysis).
                           Default False — only surfaces actionable cases.

    Returns a dict with:
        total_amount_at_risk  (actual, from case amounts)
        expected_unrecovered  (estimate, from risk scores)
        cases                 (list sorted by risk_score DESC)
        summary_by_severity   (count + amount per severity band)
        data_type             "mixed" (amounts actual, scores estimate)
    """
    cases = db.get_all_cases(conn)

    # Filter to actionable cases unless caller wants everything
    if not include_recovered:
        active = [
            c for c in cases
            if c["case_status"] not in ("recovered", "rejected", "invalid", "duplicate")
        ]
    else:
        active = [c for c in cases if c["case_status"] not in ("invalid", "duplicate")]

    if not active:
        return {
            "data_type": "mixed",
            "total_amount_at_risk": 0.0,
            "expected_unrecovered": 0.0,
            "cases": [],
            "summary_by_severity": {},
        }

    p95 = _p95_amount(active)
    scored = [score_case_risk(c, p95_amount=p95) for c in active]
    scored.sort(key=lambda r: r["risk_score"], reverse=True)

    total_at_risk = round(sum(r["amount"] for r in scored), 2)

    # Estimate unrecovered = amount × (risk_score / 100) as a rough expected loss.
    # Clearly labelled estimate — not a guaranteed figure.
    expected_unrecovered = round(
        sum(r["amount"] * r["risk_score"] / 100.0 for r in scored), 2
    )

    # Summary by severity
    severity_summary: dict = {}
    for r in scored:
        sev = r["severity"]
        b = severity_summary.setdefault(sev, {"count": 0, "amount": 0.0})
        b["count"] += 1
        b["amount"] += r["amount"]
    for b in severity_summary.values():
        b["amount"] = round(b["amount"], 2)

    return {
        "data_type": "mixed",
        "data_type_note": (
            "total_amount_at_risk is actual (from stored amounts). "
            "expected_unrecovered is an estimate derived from risk scores — "
            "not a guaranteed financial figure."
        ),
        "total_amount_at_risk": total_at_risk,
        "expected_unrecovered": expected_unrecovered,
        "active_cases": len(active),
        "cases": scored,
        "summary_by_severity": severity_summary,
    }


def top_risks(conn, limit: int = 10) -> dict:
    """Return the top N highest-risk cases for the dashboard headline panel.

    Fast path: same as revenue_at_risk but only returns the top ``limit`` cases
    and a compact summary, suitable for the Overview page without loading all case
    detail into the frontend.
    """
    full = revenue_at_risk(conn)
    top = full["cases"][:limit]
    # Compact each case to just what the headline card needs
    compact = [
        {
            "customer_id": r["customer_id"],
            "amount": r["amount"],
            "failure_reason": r["failure_reason"],
            "case_status": r["case_status"],
            "risk_score": r["risk_score"],
            "severity": r["severity"],
            "health_band": r["health_band"],
            "intervention_window": r["intervention_window"],
            "data_type": "estimate",
        }
        for r in top
    ]
    return {
        "data_type": "mixed",
        "total_amount_at_risk": full["total_amount_at_risk"],
        "expected_unrecovered": full["expected_unrecovered"],
        "active_cases": full["active_cases"],
        "top_risks": compact,
        "summary_by_severity": full["summary_by_severity"],
    }
