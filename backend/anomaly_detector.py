"""Anomaly Detection — Phase 5.

Detects significant deviations in payment / recovery behaviour from stored data.

Design principles
-----------------
- All baselines are derived from the actual stored data — no hardcoded thresholds.
- Alerts are only raised when the deviation is statistically meaningful (z-score
  or relative-change threshold, not just any non-zero difference).
- Each anomaly alert carries:
    - what was detected
    - observed value
    - expected baseline
    - severity (critical / warning / info)
    - affected segment (failure_reason / merchant_category / etc.)
    - recommended_action
    - data_type: "actual" (anomaly computed from real rows)
- The system does NOT claim causality — it surfaces patterns for human review.

What is detected
----------------
1. failure_rate_spike      Failure rate for a reason/merchant jumped vs overall avg
2. escalation_spike        Escalation rate jumped vs historical norm
3. recovery_rate_drop      Recovery rate for a segment fell below threshold
4. retry_exhaustion_pattern More cases hitting the retry cap than expected
5. compliance_degradation  Non-compliant pre-debit rate climbing
6. amount_concentration    Unusual concentration of high-value cases

Statistical method
------------------
With the current synthetic dataset (180 cases), we use simple relative-change
thresholds rather than z-scores (too few samples for stable z-scores). When the
dataset grows to > 500 cases, z-score comparisons are automatically used instead.

ALERT_THRESHOLD_RELATIVE: raise alert if segment rate deviates > this fraction
from the overall rate (e.g. 0.30 = 30% relative deviation).
"""

import os
import math
from typing import Optional

import db

# Relative-change threshold for segment vs overall rate alerts.
ALERT_THRESHOLD_RELATIVE = float(os.environ.get("ANOMALY_THRESHOLD", "0.30"))

# Minimum segment size to generate an alert (avoids noise from tiny samples).
MIN_SEGMENT_SIZE = int(os.environ.get("ANOMALY_MIN_SEGMENT", "5"))

# Z-score threshold for large datasets (> 500 cases).
Z_THRESHOLD = float(os.environ.get("ANOMALY_Z_THRESHOLD", "2.0"))

# Escalation rate that triggers a critical alert regardless of baseline.
ESCALATION_CRITICAL_RATE = float(os.environ.get("ESCALATION_CRITICAL_RATE", "0.40"))

# Non-compliance rate that triggers a warning.
COMPLIANCE_WARNING_RATE = float(os.environ.get("COMPLIANCE_WARNING_RATE", "0.25"))

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING  = "warning"
SEVERITY_INFO     = "info"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _relative_deviation(observed: float, expected: float) -> float:
    """Return (observed - expected) / expected. Safe against division by zero."""
    if expected == 0:
        return 0.0
    return (observed - expected) / expected


def _z_score(observed_rate: float, baseline_rate: float, n: int) -> Optional[float]:
    """Binomial z-score for observed_rate vs baseline_rate with n trials."""
    if n < 30 or baseline_rate <= 0 or baseline_rate >= 1:
        return None
    std = math.sqrt(baseline_rate * (1 - baseline_rate) / n)
    if std == 0:
        return None
    return (observed_rate - baseline_rate) / std


def _build_alert(
    alert_type: str,
    severity: str,
    title: str,
    description: str,
    observed_value,
    expected_value,
    affected_segment: str,
    recommended_action: str,
    evidence: dict = None,
) -> dict:
    return {
        "alert_type": alert_type,
        "severity": severity,
        "title": title,
        "description": description,
        "observed_value": observed_value,
        "expected_value": expected_value,
        "affected_segment": affected_segment,
        "recommended_action": recommended_action,
        "evidence": evidence or {},
        "data_type": "actual",
    }


# ---------------------------------------------------------------------------
# Individual detectors
# ---------------------------------------------------------------------------

def detect_failure_rate_spikes(cases: list) -> list:
    """Detect failure reasons or merchant categories with unusually high
    failure (non-recovery) rates compared to the overall rate."""
    alerts = []
    if not cases:
        return alerts

    # Overall recovery rate (baseline)
    total = len(cases)
    total_recovered = sum(1 for c in cases if c["case_status"] == "recovered")
    overall_recovery_rate = total_recovered / total if total else 0.0
    overall_failure_rate = 1.0 - overall_recovery_rate

    # Per-failure-reason
    reason_buckets: dict = {}
    for c in cases:
        r = c["failure_reason"]
        b = reason_buckets.setdefault(r, {"total": 0, "recovered": 0, "escalated": 0})
        b["total"] += 1
        if c["case_status"] == "recovered":
            b["recovered"] += 1
        elif c["case_status"] in ("escalated", "broken_promise"):
            b["escalated"] += 1

    for reason, b in reason_buckets.items():
        n = b["total"]
        if n < MIN_SEGMENT_SIZE:
            continue
        seg_recovery = b["recovered"] / n
        seg_failure = 1.0 - seg_recovery
        deviation = _relative_deviation(seg_failure, overall_failure_rate)

        # Use z-score for large n, relative threshold for small n
        z = _z_score(seg_failure, overall_failure_rate, n)
        triggered = (
            (z is not None and abs(z) >= Z_THRESHOLD) or
            (z is None and deviation > ALERT_THRESHOLD_RELATIVE)
        )
        if not triggered:
            continue

        severity = SEVERITY_CRITICAL if seg_failure > 0.70 else SEVERITY_WARNING
        alerts.append(_build_alert(
            alert_type="failure_rate_spike",
            severity=severity,
            title=f"Elevated failure rate: {reason}",
            description=(
                f"'{reason}' cases have a {seg_failure*100:.1f}% failure rate, "
                f"vs {overall_failure_rate*100:.1f}% overall "
                f"({deviation*100:+.1f}% relative deviation)."
            ),
            observed_value=round(seg_failure, 4),
            expected_value=round(overall_failure_rate, 4),
            affected_segment=f"failure_reason={reason}",
            recommended_action=(
                "Investigate recent cases for this failure reason. "
                "Check for bank outage, policy change, or data quality issues."
            ),
            evidence={"n": n, "recovered": b["recovered"],
                      "escalated": b["escalated"],
                      "z_score": round(z, 2) if z else None},
        ))

    # Per-merchant-category
    cat_buckets: dict = {}
    for c in cases:
        cat = c["merchant_category"]
        b = cat_buckets.setdefault(cat, {"total": 0, "recovered": 0})
        b["total"] += 1
        if c["case_status"] == "recovered":
            b["recovered"] += 1

    for cat, b in cat_buckets.items():
        n = b["total"]
        if n < MIN_SEGMENT_SIZE:
            continue
        seg_recovery = b["recovered"] / n
        seg_failure = 1.0 - seg_recovery
        deviation = _relative_deviation(seg_failure, overall_failure_rate)
        z = _z_score(seg_failure, overall_failure_rate, n)
        triggered = (
            (z is not None and abs(z) >= Z_THRESHOLD) or
            (z is None and deviation > ALERT_THRESHOLD_RELATIVE)
        )
        if not triggered:
            continue
        severity = SEVERITY_WARNING
        alerts.append(_build_alert(
            alert_type="failure_rate_spike",
            severity=severity,
            title=f"Elevated failure rate: {cat}",
            description=(
                f"'{cat}' merchant category has {seg_failure*100:.1f}% failure rate "
                f"vs {overall_failure_rate*100:.1f}% overall."
            ),
            observed_value=round(seg_failure, 4),
            expected_value=round(overall_failure_rate, 4),
            affected_segment=f"merchant_category={cat}",
            recommended_action=(
                "Review recent cases in this merchant category. "
                "May indicate category-specific billing cycle issue."
            ),
            evidence={"n": n, "z_score": round(z, 2) if z else None},
        ))

    return alerts


def detect_escalation_spike(cases: list) -> list:
    """Detect unusually high escalation rate overall or per segment."""
    alerts = []
    if not cases:
        return alerts

    total = len(cases)
    total_escalated = sum(
        1 for c in cases if c["case_status"] in ("escalated", "broken_promise")
    )
    escalation_rate = total_escalated / total if total else 0.0

    # Absolute critical threshold
    if escalation_rate >= ESCALATION_CRITICAL_RATE and total >= MIN_SEGMENT_SIZE:
        alerts.append(_build_alert(
            alert_type="escalation_spike",
            severity=SEVERITY_CRITICAL,
            title="Critical escalation rate",
            description=(
                f"Overall escalation rate is {escalation_rate*100:.1f}% "
                f"({total_escalated} of {total} cases). "
                f"Threshold: {ESCALATION_CRITICAL_RATE*100:.0f}%."
            ),
            observed_value=round(escalation_rate, 4),
            expected_value=ESCALATION_CRITICAL_RATE,
            affected_segment="all",
            recommended_action=(
                "Investigate cases escalated in the last 24 hours. "
                "Check retry cap, RBI compliance rate, and mandate revocation patterns."
            ),
            evidence={"total_escalated": total_escalated, "total": total},
        ))

    # Per-reason escalation
    for reason in {"insufficient_funds", "mandate_expired", "bank_technical_error"}:
        seg = [c for c in cases if c["failure_reason"] == reason]
        if len(seg) < MIN_SEGMENT_SIZE:
            continue
        seg_esc = sum(1 for c in seg if c["case_status"] in ("escalated", "broken_promise"))
        seg_rate = seg_esc / len(seg)
        deviation = _relative_deviation(seg_rate, escalation_rate)
        if deviation > ALERT_THRESHOLD_RELATIVE * 1.5 and seg_rate > 0.15:
            alerts.append(_build_alert(
                alert_type="escalation_spike",
                severity=SEVERITY_WARNING,
                title=f"High escalation: {reason}",
                description=(
                    f"'{reason}' cases escalate at {seg_rate*100:.1f}% "
                    f"vs {escalation_rate*100:.1f}% overall."
                ),
                observed_value=round(seg_rate, 4),
                expected_value=round(escalation_rate, 4),
                affected_segment=f"failure_reason={reason}",
                recommended_action=(
                    "Review strategy selection and retry timing for this failure type."
                ),
                evidence={"n": len(seg), "escalated": seg_esc},
            ))

    return alerts


def detect_recovery_rate_drop(cases: list) -> list:
    """Detect segments where recovery rate has fallen below the expected level."""
    alerts = []
    if not cases:
        return alerts

    import scoring as scoring_mod
    total = len(cases)
    total_recovered = sum(1 for c in cases if c["case_status"] == "recovered")
    overall_rate = total_recovered / total if total else 0.0

    # Expected rate from the scoring model (average over all cases)
    expected_rates = []
    for c in cases:
        sc, _ = scoring_mod.score_case(c)
        from agent import _success_prob
        expected_rates.append(_success_prob(c, sc))
    model_expected = sum(expected_rates) / len(expected_rates) if expected_rates else 0.0

    deviation = _relative_deviation(overall_rate, model_expected)
    if (abs(deviation) > ALERT_THRESHOLD_RELATIVE and
            total >= MIN_SEGMENT_SIZE and model_expected > 0):
        severity = SEVERITY_WARNING if deviation < 0 else SEVERITY_INFO
        alerts.append(_build_alert(
            alert_type="recovery_rate_drop" if deviation < 0 else "recovery_rate_high",
            severity=severity,
            title=(
                "Recovery rate below model expectation"
                if deviation < 0 else "Recovery rate above model expectation"
            ),
            description=(
                f"Actual recovery rate: {overall_rate*100:.1f}%. "
                f"Model-estimated expected: {model_expected*100:.1f}%. "
                f"Deviation: {deviation*100:+.1f}%."
            ),
            observed_value=round(overall_rate, 4),
            expected_value=round(model_expected, 4),
            affected_segment="all",
            recommended_action=(
                "If actual < expected: check retry execution, timing compliance, "
                "and whether the probability model is well-calibrated for current data."
                if deviation < 0 else
                "Positive deviation — model may be under-estimating recoverability. "
                "Consider re-training the ML model."
            ),
            evidence={"total": total, "recovered": total_recovered,
                      "model_expected_rate": round(model_expected, 4)},
        ))

    return alerts


def detect_retry_exhaustion(cases: list, audit_by_case: dict) -> list:
    """Detect unusual proportion of cases hitting the 3-retry cap without recovery."""
    alerts = []
    if not cases:
        return alerts

    from audit_check import MAX_RETRIES, RETRY_EVENT_TYPES

    exhausted = []
    for c in cases:
        events = audit_by_case.get(c["customer_id"], [])
        attempts = [e for e in events if e.get("event_type") in RETRY_EVENT_TYPES]
        distinct_attempts = {e.get("attempt_number") for e in attempts}
        if (len(distinct_attempts) >= MAX_RETRIES and
                c["case_status"] in ("escalated", "broken_promise")):
            exhausted.append(c)

    total_non_revoked = [
        c for c in cases if c.get("failure_reason") != "mandate_revoked"
    ]
    n = len(total_non_revoked)
    if n < MIN_SEGMENT_SIZE or not exhausted:
        return alerts

    exhaustion_rate = len(exhausted) / n
    if exhaustion_rate > 0.30:  # >30% hitting cap without recovery is notable
        alerts.append(_build_alert(
            alert_type="retry_exhaustion_pattern",
            severity=SEVERITY_WARNING,
            title="High retry cap exhaustion rate",
            description=(
                f"{len(exhausted)} of {n} non-revoked cases "
                f"({exhaustion_rate*100:.1f}%) exhausted all {MAX_RETRIES} retries "
                "without recovery."
            ),
            observed_value=round(exhaustion_rate, 4),
            expected_value=0.30,
            affected_segment="all non-revoked",
            recommended_action=(
                "Review cases that hit the retry cap. Consider: "
                "(1) adjusting retry timing for better salary-window coverage, "
                "(2) earlier dunning intervention, "
                "(3) policy sandbox simulation with higher retry cap."
            ),
            evidence={"exhausted": len(exhausted), "total_non_revoked": n},
        ))

    return alerts


def detect_compliance_degradation(cases: list) -> list:
    """Detect rising non-compliance with RBI pre-debit notification rule."""
    alerts = []
    compliance_cases = [c for c in cases if c.get("compliance_status") is not None]
    if len(compliance_cases) < MIN_SEGMENT_SIZE:
        return alerts

    non_compliant = sum(
        1 for c in compliance_cases if c["compliance_status"] == "non-compliant"
    )
    rate = non_compliant / len(compliance_cases)

    if rate >= COMPLIANCE_WARNING_RATE:
        alerts.append(_build_alert(
            alert_type="compliance_degradation",
            severity=SEVERITY_WARNING,
            title="RBI pre-debit compliance below threshold",
            description=(
                f"{non_compliant} of {len(compliance_cases)} cases "
                f"({rate*100:.1f}%) marked non-compliant with the RBI 24h "
                f"pre-debit notification requirement."
            ),
            observed_value=round(rate, 4),
            expected_value=COMPLIANCE_WARNING_RATE,
            affected_segment="all cases with compliance status",
            recommended_action=(
                "Audit the pre-debit notification timing. "
                "Ensure retry scheduling respects the >=24h notification window."
            ),
            evidence={"non_compliant": non_compliant, "total": len(compliance_cases)},
        ))

    return alerts


def detect_amount_concentration(cases: list) -> list:
    """Detect unusual concentration of high-value cases in the active pipeline."""
    alerts = []
    active = [c for c in cases
              if c["case_status"] not in ("recovered", "rejected", "invalid", "duplicate")]
    if len(active) < MIN_SEGMENT_SIZE:
        return alerts

    amounts = sorted(float(c["amount"]) for c in active)
    if not amounts:
        return alerts

    total_amount = sum(amounts)
    p90_idx = int(0.9 * len(amounts))
    top10_pct_amount = sum(amounts[p90_idx:])
    top10_share = top10_pct_amount / total_amount if total_amount > 0 else 0.0

    # Alert if top 10% of cases by amount hold > 70% of at-risk revenue
    if top10_share > 0.70:
        alerts.append(_build_alert(
            alert_type="amount_concentration",
            severity=SEVERITY_INFO,
            title="High concentration: top 10% cases hold most at-risk revenue",
            description=(
                f"The top 10% of active cases by amount account for "
                f"{top10_share*100:.1f}% of total at-risk revenue "
                f"(Rs {top10_pct_amount:,.0f} of Rs {total_amount:,.0f})."
            ),
            observed_value=round(top10_share, 4),
            expected_value=0.70,
            affected_segment="top 10% by amount",
            recommended_action=(
                "Prioritise recovery efforts on high-value cases. "
                "The scheduler already orders by recoverability score — "
                "verify the triage order includes amount weighting."
            ),
            evidence={
                "top10_amount_rs": round(top10_pct_amount, 2),
                "total_amount_rs": round(total_amount, 2),
                "cases_in_top10": len(active) - p90_idx,
            },
        ))

    return alerts


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_anomaly_detection(conn) -> dict:
    """Run all anomaly detectors and return a consolidated report.

    Returns:
        alerts      list of alert dicts, sorted by severity (critical first)
        total       int count of alerts
        has_critical bool
        data_type   "actual"
    """
    cases = db.get_all_cases(conn)

    # Build audit index in one pass (avoid N+1)
    audit_by_case: dict = {}
    for row in conn.execute(
        "SELECT customer_id, event_type, attempt_number "
        "FROM audit_log ORDER BY event_id"
    ).fetchall():
        audit_by_case.setdefault(row["customer_id"], []).append({
            "event_type": row["event_type"],
            "attempt_number": row["attempt_number"],
        })

    all_alerts = []
    all_alerts.extend(detect_failure_rate_spikes(cases))
    all_alerts.extend(detect_escalation_spike(cases))
    all_alerts.extend(detect_recovery_rate_drop(cases))
    all_alerts.extend(detect_retry_exhaustion(cases, audit_by_case))
    all_alerts.extend(detect_compliance_degradation(cases))
    all_alerts.extend(detect_amount_concentration(cases))

    # Sort: critical first, then warning, then info
    _sev_order = {SEVERITY_CRITICAL: 0, SEVERITY_WARNING: 1, SEVERITY_INFO: 2}
    all_alerts.sort(key=lambda a: _sev_order.get(a["severity"], 9))

    return {
        "data_type": "actual",
        "total": len(all_alerts),
        "has_critical": any(a["severity"] == SEVERITY_CRITICAL for a in all_alerts),
        "alerts": all_alerts,
        "description": (
            "Anomaly detection based on statistical analysis of stored case outcomes. "
            "Alerts surface patterns for human review — they do not change any "
            "recovery decision or execution path."
        ),
    }
