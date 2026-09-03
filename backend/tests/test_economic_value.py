"""Tests for economic_value.py — Phase 5."""

import random
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
import seed as seed_module
import agent as agent_module
import economic_value


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


def _case(failure_reason="insufficient_funds", amount=2000.0, success_rate=0.7):
    return {
        "customer_id": "EV01",
        "amount": amount,
        "failure_reason": failure_reason,
        "merchant_category": "subscription",
        "case_status": "in_progress",
        "past_payment_success_rate": success_rate,
        "past_retry_count": 0,
        "customer_tenure_months": 12,
        "mandate_limit": 5000.0,
        "history_success_days": "",
    }


# ---------------------------------------------------------------------------
# expected_value
# ---------------------------------------------------------------------------

def test_expected_value_data_type():
    case = _case()
    result = economic_value.expected_value(case, "salary-window retry")
    assert result["data_type"] == "estimate"


def test_expected_value_formula():
    """E[gross] = prob * amount; E[net] = gross - costs - friction."""
    case = _case(amount=1000.0, success_rate=0.5)
    result = economic_value.expected_value(case, "silent quick retry",
                                           override_prob=0.5)
    assert abs(result["expected_gross_value"] - 500.0) < 1.0
    assert result["expected_net_value"] < result["expected_gross_value"]
    assert result["expected_net_value"] == round(
        result["expected_gross_value"] -
        result["intervention_cost"] -
        result["friction_cost"], 4
    )


def test_expected_value_zero_prob_zero_gross():
    case = _case()
    result = economic_value.expected_value(case, "immediate escalation",
                                           override_prob=0.0)
    assert result["expected_gross_value"] == 0.0
    assert result["expected_net_value"] < 0  # costs still apply


def test_expected_value_costs_not_hardcoded():
    """Costs must come from configurable parameters, not constants."""
    case = _case()
    result = economic_value.expected_value(case, "salary-window retry")
    assumptions = result["cost_assumptions"]
    assert assumptions["retry_cost_rs"] == economic_value.RETRY_COST_RS
    assert assumptions["sms_cost_rs"] == economic_value.SMS_COST_RS


def test_silent_retry_has_no_friction():
    """Silent quick retry: no customer contact → no friction cost."""
    case = _case(failure_reason="bank_technical_error")
    result = economic_value.expected_value(case, "silent quick retry")
    assert result["friction_cost"] == 0.0


def test_expected_value_higher_amount_higher_gross():
    case_low = _case(amount=500.0)
    case_high = _case(amount=5000.0)
    prob = 0.6
    ev_low  = economic_value.expected_value(case_low,  "salary-window retry", override_prob=prob)
    ev_high = economic_value.expected_value(case_high, "salary-window retry", override_prob=prob)
    assert ev_high["expected_gross_value"] > ev_low["expected_gross_value"]


# ---------------------------------------------------------------------------
# best_strategy_by_value
# ---------------------------------------------------------------------------

def test_best_strategy_returns_applicable(run_db):
    case = _case()
    result = economic_value.best_strategy_by_value(case, run_db)
    assert result["data_type"] == "estimate"
    assert result["best_strategy"] is not None
    assert result["best_expected_net_value"] is not None


def test_best_strategy_mandate_revoked_blocked():
    """mandate_revoked must be blocked regardless of EV calculation."""
    import db as _db
    conn = _db.get_memory_connection()
    _db.init_db(conn)
    case = _case(failure_reason="mandate_revoked")
    result = economic_value.best_strategy_by_value(case, conn)
    conn.close()
    blocked = [s for s in result["all_strategies"] if s["blocked"]]
    assert len(blocked) > 0


# ---------------------------------------------------------------------------
# incremental_value
# ---------------------------------------------------------------------------

def test_incremental_value_data_type():
    case = _case()
    result = economic_value.incremental_value(case)
    assert result["data_type"] == "estimate"
    assert "ESTIMATE" in result["data_type_note"].upper()


def test_incremental_value_estimate_labelled():
    """counterfactual label must be present."""
    case = _case()
    result = economic_value.incremental_value(case)
    assert "counterfactual" in result["data_type_note"].lower()


def test_incremental_value_not_hardcoded():
    """Value must vary with the case, not be a fixed constant."""
    case_a = _case(amount=500.0, success_rate=0.3)
    case_b = _case(amount=5000.0, success_rate=0.9)
    result_a = economic_value.incremental_value(case_a)
    result_b = economic_value.incremental_value(case_b)
    assert result_a["incremental_expected_value"] != result_b["incremental_expected_value"]


# ---------------------------------------------------------------------------
# portfolio_ev
# ---------------------------------------------------------------------------

def test_portfolio_ev_data_type(run_db):
    result = economic_value.portfolio_ev(run_db)
    assert result["data_type"] == "mixed"


def test_portfolio_ev_amount_at_risk_actual(run_db):
    """total_amount_at_risk is from real case amounts — not invented."""
    import metrics
    core = metrics.core_metrics(run_db)
    result = economic_value.portfolio_ev(run_db)
    # Active cases = all cases minus recovered/rejected/invalid/duplicate
    cases = db.get_all_cases(run_db)
    active_amount = sum(
        float(c["amount"]) for c in cases
        if c["case_status"] not in ("recovered", "rejected", "invalid", "duplicate")
    )
    assert abs(result["total_amount_at_risk"] - active_amount) < 0.02


def test_portfolio_ev_net_value_less_than_gross(run_db):
    result = economic_value.portfolio_ev(run_db)
    assert result["total_expected_net_value"] < result["total_expected_gross_value"]


def test_portfolio_ev_cost_assumptions_visible(run_db):
    result = economic_value.portfolio_ev(run_db)
    assert "cost_assumptions" in result
    assert result["cost_assumptions"]["retry_cost_rs"] == economic_value.RETRY_COST_RS
