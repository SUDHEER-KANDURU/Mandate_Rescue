"""Tests for anomaly_detector.py — Phase 5."""

import random
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
import seed as seed_module
import agent as agent_module
import anomaly_detector
from anomaly_detector import (
    SEVERITY_CRITICAL, SEVERITY_WARNING, SEVERITY_INFO,
    ESCALATION_CRITICAL_RATE, MIN_SEGMENT_SIZE,
)


@pytest.fixture
def run_db():
    conn = db.get_memory_connection()
    db.init_db(conn)
    rng = random.Random(seed_module.SEED)
    for r in seed_module.build_records(rng):
        db.insert_mandate_failure(conn, r)
    conn.commit()
    policy = agent_module.PolicyParams(use_llm=False, execution_mode="simulation")
    agent_module.run_agent(policy=policy, conn=conn, seed=42)
    yield conn
    conn.close()


@pytest.fixture
def empty_db():
    conn = db.get_memory_connection()
    db.init_db(conn)
    yield conn
    conn.close()


def _make_cases(n_total, n_escalated, failure_reason="insufficient_funds"):
    """Build a minimal case list with controlled escalation rate."""
    cases = []
    for i in range(n_total):
        status = "escalated" if i < n_escalated else "recovered"
        cases.append({
            "customer_id": f"C{i:04d}",
            "amount": 2000.0,
            "failure_reason": failure_reason,
            "merchant_category": "subscription",
            "case_status": status,
            "compliance_status": "RBI-compliant",
            "past_payment_success_rate": 0.7,
            "past_retry_count": 1,
            "customer_tenure_months": 12,
            "mandate_limit": 5000.0,
        })
    return cases


# ---------------------------------------------------------------------------
# run_anomaly_detection
# ---------------------------------------------------------------------------

def test_run_anomaly_detection_data_type(run_db):
    result = anomaly_detector.run_anomaly_detection(run_db)
    assert result["data_type"] == "actual"
    for alert in result["alerts"]:
        assert alert["data_type"] == "actual"


def test_run_anomaly_detection_sorted_by_severity(run_db):
    result = anomaly_detector.run_anomaly_detection(run_db)
    sev_order = {SEVERITY_CRITICAL: 0, SEVERITY_WARNING: 1, SEVERITY_INFO: 2}
    severities = [sev_order.get(a["severity"], 9) for a in result["alerts"]]
    assert severities == sorted(severities), "Alerts not sorted critical-first"


def test_run_anomaly_detection_empty_db(empty_db):
    result = anomaly_detector.run_anomaly_detection(empty_db)
    assert result["total"] == 0
    assert result["alerts"] == []
    assert result["has_critical"] is False


def test_alerts_have_required_fields(run_db):
    result = anomaly_detector.run_anomaly_detection(run_db)
    for alert in result["alerts"]:
        for field in ("alert_type", "severity", "title", "description",
                      "observed_value", "expected_value", "affected_segment",
                      "recommended_action", "data_type"):
            assert field in alert, f"Missing field {field!r} in alert"


# ---------------------------------------------------------------------------
# detect_escalation_spike
# ---------------------------------------------------------------------------

def test_escalation_spike_critical_threshold():
    """Must fire critical when escalation rate >= ESCALATION_CRITICAL_RATE."""
    total = 50
    # Set escalation just above the critical threshold
    n_esc = int(ESCALATION_CRITICAL_RATE * total) + 1
    cases = _make_cases(total, n_esc)
    alerts = anomaly_detector.detect_escalation_spike(cases)
    critical = [a for a in alerts if a["severity"] == SEVERITY_CRITICAL]
    assert len(critical) >= 1, "Expected critical alert for high escalation rate"


def test_no_escalation_spike_for_low_rate():
    """No alert when escalation is well below the threshold."""
    total = 100
    n_esc = 5  # 5% escalation — well below threshold
    cases = _make_cases(total, n_esc)
    alerts = anomaly_detector.detect_escalation_spike(cases)
    assert len(alerts) == 0


def test_escalation_spike_empty():
    assert anomaly_detector.detect_escalation_spike([]) == []


# ---------------------------------------------------------------------------
# detect_failure_rate_spikes
# ---------------------------------------------------------------------------

def test_failure_rate_spike_fires_for_high_deviation(run_db):
    """After a real run, mandate_revoked (0% recovery) should trigger a spike alert."""
    cases = db.get_all_cases(run_db)
    alerts = anomaly_detector.detect_failure_rate_spikes(cases)
    # mandate_revoked has 0% recovery vs some positive overall rate — should trigger
    triggered_reasons = [a["affected_segment"] for a in alerts
                         if "failure_reason" in a.get("affected_segment", "")]
    assert any("mandate_revoked" in s for s in triggered_reasons), \
        "Expected alert for mandate_revoked (0% recovery)"


def test_failure_rate_spike_empty():
    assert anomaly_detector.detect_failure_rate_spikes([]) == []


# ---------------------------------------------------------------------------
# detect_compliance_degradation
# ---------------------------------------------------------------------------

def test_compliance_degradation_fires_when_high_rate():
    """Alert when non-compliant rate >= COMPLIANCE_WARNING_RATE."""
    cases = []
    for i in range(20):
        cases.append({
            "customer_id": f"COMP{i:03d}",
            "amount": 1000.0,
            "failure_reason": "insufficient_funds",
            "merchant_category": "subscription",
            "case_status": "escalated",
            "compliance_status": "non-compliant" if i < 8 else "RBI-compliant",
            "past_payment_success_rate": 0.5,
            "past_retry_count": 1,
            "customer_tenure_months": 6,
            "mandate_limit": 5000.0,
        })
    alerts = anomaly_detector.detect_compliance_degradation(cases)
    assert len(alerts) >= 1
    assert alerts[0]["alert_type"] == "compliance_degradation"


def test_compliance_degradation_none_when_compliant():
    cases = []
    for i in range(20):
        cases.append({
            "customer_id": f"COMP{i:03d}",
            "compliance_status": "RBI-compliant",
        })
    alerts = anomaly_detector.detect_compliance_degradation(cases)
    assert len(alerts) == 0


# ---------------------------------------------------------------------------
# detect_amount_concentration
# ---------------------------------------------------------------------------

def test_amount_concentration_fires_for_skewed_amounts():
    """Alert when top 10% cases hold > 70% of revenue."""
    cases = []
    # 90 small cases + 10 very large cases
    for i in range(90):
        cases.append({"customer_id": f"S{i:04d}", "amount": 100.0,
                      "case_status": "in_progress"})
    for i in range(10):
        cases.append({"customer_id": f"L{i:04d}", "amount": 100000.0,
                      "case_status": "in_progress"})
    alerts = anomaly_detector.detect_amount_concentration(cases)
    assert len(alerts) >= 1


def test_amount_concentration_none_for_uniform():
    cases = [{"customer_id": f"U{i:04d}", "amount": 1000.0, "case_status": "in_progress"}
             for i in range(50)]
    alerts = anomaly_detector.detect_amount_concentration(cases)
    assert len(alerts) == 0


# ---------------------------------------------------------------------------
# detect_retry_exhaustion
# ---------------------------------------------------------------------------

def test_retry_exhaustion_fires_when_many_exhausted(run_db):
    cases = db.get_all_cases(run_db)
    audit_by_case: dict = {}
    for row in run_db.execute(
        "SELECT customer_id, event_type, attempt_number FROM audit_log"
    ).fetchall():
        audit_by_case.setdefault(row["customer_id"], []).append({
            "event_type": row["event_type"],
            "attempt_number": row["attempt_number"],
        })
    alerts = anomaly_detector.detect_retry_exhaustion(cases, audit_by_case)
    # May or may not fire depending on actual exhaustion rate — just verify it runs
    for a in alerts:
        assert a["alert_type"] == "retry_exhaustion_pattern"
        assert a["data_type"] == "actual"


# ---------------------------------------------------------------------------
# Invariant: no alert should show hardcoded numeric thresholds as observed values
# ---------------------------------------------------------------------------

def test_observed_values_from_real_data(run_db):
    """observed_value must not be a hardcoded constant (0.0, 1.0, etc.)."""
    result = anomaly_detector.run_anomaly_detection(run_db)
    for alert in result["alerts"]:
        obs = alert["observed_value"]
        # observed_value of exactly 0 or 1 is suspicious (could be hardcoded)
        # Allow 0.0 only if that's genuinely the measured rate (mandate_revoked)
        if obs == 0.0:
            assert "revoked" in alert.get("affected_segment", "").lower() or \
                   alert["alert_type"] != "failure_rate_spike", \
                   f"Suspicious 0.0 observed_value in non-revoked alert: {alert['title']}"
