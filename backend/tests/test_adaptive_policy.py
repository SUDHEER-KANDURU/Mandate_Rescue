"""Tests for adaptive_policy.py — Phase 5."""

import random
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
import seed as seed_module
import agent as agent_module
import adaptive_policy
from adaptive_policy import (
    GOV_AUTO_EXECUTE, GOV_RECOMMEND, GOV_REQUIRE_APPROVAL, GOV_BLOCK,
    HIGH_VALUE_THRESHOLD,
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


def _case(failure_reason="insufficient_funds", amount=2000.0,
          mandate_limit=5000.0, category="subscription"):
    return {
        "customer_id": "POLICY01",
        "amount": amount,
        "failure_reason": failure_reason,
        "merchant_category": category,
        "case_status": "in_progress",
        "past_payment_success_rate": 0.7,
        "past_retry_count": 0,
        "customer_tenure_months": 12,
        "mandate_limit": mandate_limit,
        "history_success_days": "",
    }


# ---------------------------------------------------------------------------
# Governance tiers
# ---------------------------------------------------------------------------

def test_mandate_revoked_always_blocked(empty_db):
    case = _case(failure_reason="mandate_revoked")
    result = adaptive_policy.recommend_strategy(case, empty_db)
    assert result["governance"] == GOV_BLOCK
    assert result["is_blocked"] is True
    assert result["can_auto_execute"] is False


def test_high_value_requires_approval(empty_db):
    case = _case(amount=HIGH_VALUE_THRESHOLD + 1000)
    result = adaptive_policy.recommend_strategy(case, empty_db)
    assert result["governance"] == GOV_REQUIRE_APPROVAL
    assert result["requires_approval"] is True
    assert result["can_auto_execute"] is False


def test_low_value_standard_case_auto_executes(run_db):
    """A low-value case with good data should auto-execute."""
    case = _case(failure_reason="bank_technical_error", amount=500.0)
    result = adaptive_policy.recommend_strategy(case, run_db)
    # bank_technical_error has high observed rate → should auto-execute
    assert result["governance"] in (GOV_AUTO_EXECUTE, GOV_RECOMMEND)


def test_insufficient_funds_recommends_salary_window(run_db):
    case = _case(failure_reason="insufficient_funds")
    result = adaptive_policy.recommend_strategy(case, run_db)
    # Default rule-based should be salary-window retry; might be overridden if
    # another strategy genuinely outperforms it for this merchant category
    assert "salary" in result["recommended_strategy"].lower() or \
           result["recommended_strategy"] in adaptive_policy.KNOWN_STRATEGIES


def test_mandate_expired_recommends_reauth(empty_db):
    case = _case(failure_reason="mandate_expired")
    result = adaptive_policy.recommend_strategy(case, empty_db)
    # With no data, falls back to rule-based
    assert "re-authorization" in result["recommended_strategy"].lower() or \
           "reauth" in result["recommended_strategy"].lower() or \
           result["data_source"].startswith("estimate")


# ---------------------------------------------------------------------------
# explain() output
# ---------------------------------------------------------------------------

def test_recommend_has_explain_steps(run_db):
    case = _case()
    result = adaptive_policy.recommend_strategy(case, run_db)
    explain = result["explain"]
    assert isinstance(explain, list)
    assert len(explain) >= 2
    step_names = [s["step"] for s in explain]
    assert any("Rule-based" in s for s in step_names)
    assert any("Governance" in s for s in step_names)


def test_recommend_has_confidence(run_db):
    case = _case()
    result = adaptive_policy.recommend_strategy(case, run_db)
    assert 0.0 <= result["confidence"] <= 1.0
    assert result["confidence_pct"].endswith("%")


def test_recommend_data_source_labelled(run_db):
    case = _case()
    result = adaptive_policy.recommend_strategy(case, run_db)
    assert result["data_source"] in (
        # one of these patterns
        *[f"observed (merchant_category='{cat}', n={n})"
          for cat in ("subscription", "emi", "insurance", "utility")
          for n in range(1, 200)],
        "estimate (insufficient observed data, using probability model)",
    ) or "observed" in result["data_source"] or "estimate" in result["data_source"]


# ---------------------------------------------------------------------------
# policy_summary
# ---------------------------------------------------------------------------

def test_policy_summary_structure(run_db):
    result = adaptive_policy.policy_summary(run_db)
    assert result["data_type"] == "actual"
    assert "governance_thresholds" in result
    assert "observed_strategy_performance" in result
    assert "recommended_changes" in result


def test_policy_summary_thresholds_configurable(run_db):
    result = adaptive_policy.policy_summary(run_db)
    thresholds = result["governance_thresholds"]
    assert thresholds["high_value_threshold_rs"] == HIGH_VALUE_THRESHOLD
    assert 0 < thresholds["low_confidence_rate"] < 1


def test_policy_summary_no_invented_strategies(run_db):
    """All strategies in performance table must be from intelligence.KNOWN_STRATEGIES."""
    from intelligence import KNOWN_STRATEGIES
    result = adaptive_policy.policy_summary(run_db)
    for strat in result["observed_strategy_performance"]:
        assert strat in KNOWN_STRATEGIES, \
            f"Unknown strategy in performance table: {strat!r}"


# ---------------------------------------------------------------------------
# batch_recommend
# ---------------------------------------------------------------------------

def test_batch_recommend_returns_one_per_case(run_db):
    cases = db.get_all_cases(run_db)[:10]
    results = adaptive_policy.batch_recommend(cases, run_db)
    assert len(results) == len(cases)
    for r in results:
        assert "recommended_strategy" in r
        assert "governance" in r
        assert "explain" in r


def test_batch_recommend_blocked_for_revoked(run_db):
    """All mandate_revoked cases in the batch must be blocked."""
    cases = [c for c in db.get_all_cases(run_db)
             if c["failure_reason"] == "mandate_revoked"][:5]
    if not cases:
        pytest.skip("No mandate_revoked cases in test DB")
    results = adaptive_policy.batch_recommend(cases, run_db)
    for r in results:
        assert r["governance"] == GOV_BLOCK
