"""Unit tests for the two baseline simulations (baseline.py)."""

import baseline
from agent import MAX_RETRIES


def test_naive_baseline_shape(fresh_db):
    result = baseline.run_baseline(fresh_db)
    assert result["total_cases"] == 180
    assert 0 <= result["recovery_rate"] <= 1
    assert result["amount_recovered"] <= result["amount_at_risk"]


def test_dumb_persistence_baseline_shape(fresh_db):
    result = baseline.run_dumb_persistence_baseline(fresh_db)
    assert result["total_cases"] == 180
    assert result["retry_cap"] == MAX_RETRIES
    assert result["attempts_used"] > result["total_cases"]  # more than 1 try/case on average
    assert 0 <= result["recovery_rate"] <= 1


def test_dumb_persistence_recovers_at_least_as_much_as_naive(fresh_db):
    """More attempts at the same per-attempt probability can only help or tie, never
    hurt, on the SAME probability model — this is a sanity check on the comparison's
    internal consistency, not a live-agent guarantee (different RNG streams could in
    principle disagree on a tiny sample, but at 180 cases with a 3x attempt budget
    the dumb-persistence recovery rate is expected to clearly exceed the 1-attempt
    naive baseline)."""
    naive = baseline.run_baseline(fresh_db)
    dumb = baseline.run_dumb_persistence_baseline(fresh_db)
    assert dumb["recovered_cases"] >= naive["recovered_cases"]


def test_dumb_persistence_respects_custom_retry_cap(fresh_db):
    result = baseline.run_dumb_persistence_baseline(fresh_db, retry_cap=1)
    assert result["retry_cap"] == 1
    assert result["attempts_used"] == result["total_cases"]


def test_baselines_are_pure_no_side_effects(fresh_db):
    """Neither baseline may write to audit_log or mutate case_status."""
    import db
    before_cases = db.get_all_cases(fresh_db)
    before_audit_count = len(db.get_all_audit(fresh_db))
    baseline.run_baseline(fresh_db)
    baseline.run_dumb_persistence_baseline(fresh_db)
    after_cases = db.get_all_cases(fresh_db)
    after_audit_count = len(db.get_all_audit(fresh_db))
    assert after_audit_count == before_audit_count == 0
    before_statuses = {c["customer_id"]: c["case_status"] for c in before_cases}
    after_statuses = {c["customer_id"]: c["case_status"] for c in after_cases}
    assert before_statuses == after_statuses
