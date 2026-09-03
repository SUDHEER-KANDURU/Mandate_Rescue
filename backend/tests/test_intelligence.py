"""Tests for intelligence.py — Phase 5.

Verifies that every aggregate function:
1. Returns real data (not hardcoded values) from in-memory seeded cases.
2. Carries the correct data_type labels ("actual", "estimate", "simulation", "mixed").
3. Never returns hardcoded outcomes — all values must come from the actual case data.
4. Handles the empty-DB edge case without crashing.
5. Strategy performance is extracted from audit_log, not invented.
6. Counterfactual / incremental values are clearly labelled as estimates.
"""

import random
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
import seed as seed_module
import agent as agent_module
import intelligence


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def empty_db():
    conn = db.get_memory_connection()
    db.init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def seeded_db():
    """180 cases seeded but agent NOT yet run — case_status='new'."""
    conn = db.get_memory_connection()
    db.init_db(conn)
    rng = random.Random(seed_module.SEED)
    for r in seed_module.build_records(rng):
        db.insert_mandate_failure(conn, r)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def run_db():
    """180 cases seeded AND agent run — real outcomes in mandate_failures."""
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


# ---------------------------------------------------------------------------
# by_failure_reason
# ---------------------------------------------------------------------------

def test_by_failure_reason_data_type(run_db):
    result = intelligence.by_failure_reason(run_db)
    assert result["data_type"] == "actual"
    for row in result["by_failure_reason"]:
        assert row["data_type"] == "actual"


def test_by_failure_reason_not_hardcoded(run_db):
    """Values must come from actual case outcomes, not fixed constants."""
    result = intelligence.by_failure_reason(run_db)
    rows = result["by_failure_reason"]
    assert len(rows) > 0
    # Each row must have a real count — not a constant like 0 or 180
    totals = [r["total"] for r in rows]
    assert sum(totals) == len(db.get_all_cases(run_db))
    # Recovery rates must vary across reasons (not all the same constant)
    rates = [r["recovery_rate"] for r in rows]
    assert len(set(rates)) > 1, "All recovery rates identical — likely hardcoded"


def test_by_failure_reason_has_prior_comparison(run_db):
    """Each row should carry a comparison to the scoring.py expert prior."""
    result = intelligence.by_failure_reason(run_db)
    for row in result["by_failure_reason"]:
        assert "recoverability_prior" in row
        assert "prior_vs_actual_delta" in row


def test_by_failure_reason_revoked_zero_rate(run_db):
    """mandate_revoked cases must have 0% recovery (policy: no retry)."""
    result = intelligence.by_failure_reason(run_db)
    revoked = next(
        (r for r in result["by_failure_reason"] if r["segment"] == "mandate_revoked"),
        None,
    )
    assert revoked is not None
    assert revoked["recovery_rate"] == 0.0, "mandate_revoked should have 0% recovery"
    assert revoked["recovered"] == 0


def test_by_failure_reason_empty_db(empty_db):
    result = intelligence.by_failure_reason(empty_db)
    assert result["by_failure_reason"] == []


# ---------------------------------------------------------------------------
# by_strategy_outcome
# ---------------------------------------------------------------------------

def test_by_strategy_outcome_data_type(run_db):
    result = intelligence.by_strategy_outcome(run_db)
    assert result["data_type"] == "actual"
    for row in result["by_strategy"]:
        assert row["data_type"] == "actual"


def test_by_strategy_outcome_from_audit_log(run_db):
    """Strategy labels must be extracted from audit_log, not made up."""
    result = intelligence.by_strategy_outcome(run_db)
    rows = result["by_strategy"]
    assert len(rows) > 0
    # All known strategy labels come from the agent
    from intelligence import KNOWN_STRATEGIES
    for row in rows:
        assert row["strategy"] in KNOWN_STRATEGIES, \
            f"Unknown strategy in output: {row['strategy']!r}"


def test_by_strategy_immediate_escalation_zero_recovery(run_db):
    """immediate escalation strategy should have 0% recovery (mandate_revoked path)."""
    result = intelligence.by_strategy_outcome(run_db)
    esc_row = next(
        (r for r in result["by_strategy"] if r["strategy"] == "immediate escalation"),
        None,
    )
    if esc_row and esc_row["sufficient_sample"]:
        assert esc_row["recovery_rate"] == 0.0


def test_by_strategy_outcome_empty_db(empty_db):
    result = intelligence.by_strategy_outcome(empty_db)
    assert result["by_strategy"] == []
    assert result["cases_without_strategy"] == 0


# ---------------------------------------------------------------------------
# incremental_revenue
# ---------------------------------------------------------------------------

def test_incremental_revenue_data_types(run_db):
    result = intelligence.incremental_revenue(run_db)
    assert result["data_type"] == "mixed"
    assert result["actual"]["data_type"] == "actual"
    assert result["naive_baseline_1_attempt"]["data_type"] == "estimate"
    assert result["dumb_persistence_baseline"]["data_type"] == "estimate"
    assert result["incremental"]["data_type"] == "estimate"


def test_incremental_revenue_actual_matches_metrics(run_db):
    """The 'actual' section must match metrics.core_metrics()."""
    import metrics
    result = intelligence.incremental_revenue(run_db)
    core = metrics.core_metrics(run_db)
    assert abs(result["actual"]["amount_recovered"] - core["amount_recovered"]) < 0.01


def test_incremental_revenue_estimate_labelled(run_db):
    """Incremental interpretation must contain [ESTIMATE] label."""
    result = intelligence.incremental_revenue(run_db)
    assert "ESTIMATE" in result["incremental"]["interpretation"].upper()


def test_incremental_revenue_empty_db(empty_db):
    result = intelligence.incremental_revenue(empty_db)
    assert result["actual"]["amount_recovered"] == 0.0
    assert result["actual"]["total_cases"] == 0


# ---------------------------------------------------------------------------
# merchant_learning
# ---------------------------------------------------------------------------

def test_merchant_learning_data_type(run_db):
    result = intelligence.merchant_learning(run_db)
    assert result["data_type"] == "actual"


def test_merchant_learning_requires_minimum_sample(run_db):
    """Recommendations only for merchants with sufficient data."""
    result = intelligence.merchant_learning(run_db)
    for m in result["merchants"]:
        if not m["sufficient_data"]:
            assert m["best_strategy"] is None or m["recommendation"].startswith("Insufficient")


def test_merchant_learning_no_invented_strategies(run_db):
    """Recommended strategies must be ones actually observed in audit_log."""
    result = intelligence.by_strategy_outcome(run_db)
    observed = {r["strategy"] for r in result["by_strategy"]}
    ml = intelligence.merchant_learning(run_db)
    for m in ml["merchants"]:
        if m["best_strategy"]:
            assert m["best_strategy"] in observed, \
                f"Recommended strategy '{m['best_strategy']}' not in observed strategies"


# ---------------------------------------------------------------------------
# failure_rate_by_segment
# ---------------------------------------------------------------------------

def test_failure_rate_by_segment_actual(run_db):
    result = intelligence.failure_rate_by_segment(run_db)
    assert result["data_type"] == "actual"
    for seg in result["segments"]:
        assert seg["data_type"] == "actual"
        assert 0.0 <= seg["recovery_rate"] <= 1.0


def test_failure_rate_by_segment_totals_consistent(run_db):
    """Sum of segment totals must equal total case count."""
    cases = db.get_all_cases(run_db)
    result = intelligence.failure_rate_by_segment(run_db)
    total_from_segments = sum(s["total"] for s in result["segments"])
    assert total_from_segments == len(cases)


# ---------------------------------------------------------------------------
# full_summary
# ---------------------------------------------------------------------------

def test_full_summary_all_sections_present(run_db):
    result = intelligence.full_summary(run_db, include_simulation=False)
    required = [
        "by_failure_reason", "by_strategy_outcome", "by_merchant_category",
        "failure_rate_by_segment", "incremental_revenue", "merchant_learning",
    ]
    for key in required:
        assert key in result, f"Missing section: {key}"


def test_full_summary_simulation_labelled(run_db):
    result = intelligence.full_summary(run_db, include_simulation=True)
    assert "strategy_comparison" in result
    sim = result["strategy_comparison"]
    assert sim["data_type"] == "simulation"
    for strat in sim["strategies"]:
        assert strat["data_type"] == "simulation"
        assert "SIMULATION" in strat["label"].upper()
