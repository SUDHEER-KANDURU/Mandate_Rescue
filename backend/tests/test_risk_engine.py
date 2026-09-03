"""Tests for risk_engine.py — Phase 5."""

import random
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
import seed as seed_module
import agent as agent_module
import risk_engine


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
def seeded_db():
    conn = db.get_memory_connection()
    db.init_db(conn)
    rng = random.Random(seed_module.SEED)
    for r in seed_module.build_records(rng):
        db.insert_mandate_failure(conn, r)
    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# score_case_risk
# ---------------------------------------------------------------------------

def test_score_case_risk_data_type():
    case = {
        "customer_id": "TEST001",
        "amount": 3000.0,
        "failure_reason": "insufficient_funds",
        "merchant_category": "subscription",
        "case_status": "in_progress",
        "past_payment_success_rate": 0.6,
        "past_retry_count": 1,
        "customer_tenure_months": 12,
        "mandate_limit": 5000.0,
        "history_success_days": "",
    }
    result = risk_engine.score_case_risk(case)
    assert result["data_type"] == "estimate"
    assert 0 <= result["risk_score"] <= 100
    assert 0 <= result["recoverability_score"] <= 100


def test_mandate_revoked_high_risk():
    """mandate_revoked must have severity='critical' and high risk score."""
    case = {
        "customer_id": "REVOKED01",
        "amount": 5000.0,
        "failure_reason": "mandate_revoked",
        "merchant_category": "subscription",
        "case_status": "new",
        "past_payment_success_rate": 0.9,
        "past_retry_count": 0,
        "customer_tenure_months": 24,
        "mandate_limit": 5000.0,
        "history_success_days": "",
    }
    result = risk_engine.score_case_risk(case)
    assert result["severity"] == "critical"
    assert result["intervention_window"]["type"] == "none"


def test_bank_technical_error_low_severity():
    case = {
        "customer_id": "BANK01",
        "amount": 1000.0,
        "failure_reason": "bank_technical_error",
        "merchant_category": "subscription",
        "case_status": "new",
        "past_payment_success_rate": 0.9,
        "past_retry_count": 0,
        "customer_tenure_months": 12,
        "mandate_limit": 5000.0,
        "history_success_days": "",
    }
    result = risk_engine.score_case_risk(case)
    assert result["severity"] == "low"


def test_over_limit_adds_urgency():
    base_case = {
        "customer_id": "LIMIT01",
        "amount": 3000.0,
        "failure_reason": "insufficient_funds",
        "merchant_category": "subscription",
        "case_status": "new",
        "past_payment_success_rate": 0.6,
        "past_retry_count": 0,
        "customer_tenure_months": 6,
        "mandate_limit": 5000.0,
        "history_success_days": "",
    }
    over_case = dict(base_case, amount=6000.0)  # over the limit
    base_result = risk_engine.score_case_risk(base_case, p95_amount=6000.0)
    over_result = risk_engine.score_case_risk(over_case, p95_amount=6000.0)
    assert over_result["over_limit"] is True
    assert over_result["risk_score"] > base_result["risk_score"]


def test_risk_score_bounds():
    """Risk score must always be in 0–100."""
    for amount in [100, 5000, 100000]:
        for reason in ["insufficient_funds", "mandate_expired",
                       "bank_technical_error", "mandate_revoked"]:
            case = {
                "customer_id": "BOUNDS01",
                "amount": float(amount),
                "failure_reason": reason,
                "merchant_category": "subscription",
                "case_status": "new",
                "past_payment_success_rate": 0.5,
                "past_retry_count": 1,
                "customer_tenure_months": 6,
                "mandate_limit": 5000.0,
                "history_success_days": "",
            }
            result = risk_engine.score_case_risk(case)
            assert 0 <= result["risk_score"] <= 100, \
                f"risk_score out of range for amount={amount} reason={reason}"


def test_contributing_factors_present():
    case = {
        "customer_id": "CF01",
        "amount": 2500.0,
        "failure_reason": "insufficient_funds",
        "merchant_category": "emi",
        "case_status": "in_progress",
        "past_payment_success_rate": 0.4,
        "past_retry_count": 2,
        "customer_tenure_months": 3,
        "mandate_limit": 5000.0,
        "history_success_days": "",
    }
    result = risk_engine.score_case_risk(case)
    factors = result["contributing_factors"]
    assert len(factors) >= 3
    factor_names = [f["factor"] for f in factors]
    assert any("Recoverability" in n for n in factor_names)
    assert any("Failure reason" in n for n in factor_names)


# ---------------------------------------------------------------------------
# revenue_at_risk
# ---------------------------------------------------------------------------

def test_revenue_at_risk_data_type(seeded_db):
    result = risk_engine.revenue_at_risk(seeded_db)
    assert result["data_type"] == "mixed"
    assert result["total_amount_at_risk"] > 0


def test_revenue_at_risk_excludes_resolved(run_db):
    """Recovered cases must not appear in the active risk list by default."""
    result = risk_engine.revenue_at_risk(run_db, include_recovered=False)
    for case_risk in result["cases"]:
        assert case_risk["case_status"] != "recovered", \
            "Recovered case appeared in active risk list"


def test_revenue_at_risk_sorted_by_score(seeded_db):
    result = risk_engine.revenue_at_risk(seeded_db)
    scores = [r["risk_score"] for r in result["cases"]]
    assert scores == sorted(scores, reverse=True), "Cases not sorted by risk_score DESC"


def test_expected_unrecovered_is_estimate(seeded_db):
    result = risk_engine.revenue_at_risk(seeded_db)
    # expected_unrecovered is explicitly labelled as estimate
    assert "estimate" in result["data_type_note"].lower()
    # Must be <= total_amount_at_risk (can't expect to lose more than you have)
    assert result["expected_unrecovered"] <= result["total_amount_at_risk"]


def test_top_risks_compact(seeded_db):
    result = risk_engine.top_risks(seeded_db, limit=5)
    assert len(result["top_risks"]) <= 5
    for r in result["top_risks"]:
        assert r["data_type"] == "estimate"
        assert "risk_score" in r
        assert "intervention_window" in r


def test_revenue_at_risk_empty_db():
    conn = db.get_memory_connection()
    db.init_db(conn)
    result = risk_engine.revenue_at_risk(conn)
    assert result["total_amount_at_risk"] == 0.0
    assert result["cases"] == []
    conn.close()
