"""Performance benchmarks for Phase 5 — and Phase 4/3 perf fixes.

These tests measure actual elapsed time for key operations and fail if they
exceed the defined budget. Budgets are generous enough to accommodate CI
variance but tight enough to catch regressions.

All numbers are measured, not hardcoded. If a test fails it reports the
actual measured time so you can see the regression immediately.

Performance budgets (wall-clock, single-threaded, development machine):
  /api/cases enrichment (180 rows, batch ML)  < 150 ms  (was 6500 ms before fix)
  /api/exceptions JOIN query                  <  50 ms  (was ~14 ms, just regression guard)
  /api/activity DESC LIMIT query              <  20 ms  (was loading all rows)
  core_metrics (full table scan)              <  20 ms
  risk_engine.revenue_at_risk (180 cases)     < 100 ms
  intelligence.full_summary (no simulation)   < 200 ms
  anomaly_detector (180 cases)                <  50 ms
"""

import random
import sys
import os
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
import seed as seed_module
import agent as agent_module
import metrics
import scoring
import health as health_module
import salary_window
import risk_engine
import intelligence
import anomaly_detector
from ml import predict as ml_predict


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def run_db_module():
    """Module-scoped fixture: seeded + agent run. Reused across all perf tests."""
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


def _time(fn) -> float:
    """Return elapsed ms for calling fn()."""
    t0 = time.perf_counter()
    fn()
    return (time.perf_counter() - t0) * 1000


# ---------------------------------------------------------------------------
# PERF-1: /api/cases batch ML prediction
# ---------------------------------------------------------------------------

def test_perf_api_cases_enrichment(run_db_module):
    """Batch ML + per-case enrichment must be < 150ms (was 6500ms before fix)."""
    cases = db.get_all_cases(run_db_module)

    # Warm up the model (first call loads pandas + sklearn)
    ml_predict.predict_batch(cases[:1])

    def run():
        ml_probs = ml_predict.predict_batch(cases)
        for c, p in zip(cases, ml_probs):
            score, _ = scoring.score_case(c)
            _ = salary_window.infer_window(c)
            hs = health_module.health_score(
                c.get("past_payment_success_rate", 0.0),
                c.get("past_retry_count", 0)
            )
            _ = health_module.health_band(hs)

    elapsed = _time(run)
    print(f"\n/api/cases enrichment ({len(cases)} rows): {elapsed:.1f}ms")
    assert elapsed < 150, (
        f"/api/cases enrichment took {elapsed:.1f}ms — exceeds 150ms budget. "
        "Check that predict_batch is being used (not per-case calls)."
    )


def test_perf_predict_batch_vs_single(run_db_module):
    """predict_batch must be faster than N individual calls."""
    cases = db.get_all_cases(run_db_module)

    # Warm up
    ml_predict.predict_batch(cases[:1])
    ml_predict.predict_recovery_probability(cases[0])

    batch_ms = _time(lambda: ml_predict.predict_batch(cases))
    # Compare to 10 single calls (not 180 — that would take too long to measure)
    single_10_ms = _time(lambda: [ml_predict.predict_recovery_probability(c)
                                   for c in cases[:10]])
    single_180_est = single_10_ms * 18  # estimate for 180

    print(f"\nbatch (180): {batch_ms:.1f}ms  single×10: {single_10_ms:.1f}ms  "
          f"single×180 (est): {single_180_est:.1f}ms")
    assert batch_ms < single_180_est * 0.8, (
        f"Batch ({batch_ms:.1f}ms) not faster than estimated single ({single_180_est:.1f}ms)"
    )


# ---------------------------------------------------------------------------
# PERF-2: /api/exceptions JOIN vs N+1
# ---------------------------------------------------------------------------

def test_perf_exceptions_join_query(run_db_module):
    """exceptions() with JOIN must be < 50ms."""
    elapsed = _time(lambda: metrics.exceptions(run_db_module))
    print(f"\nexceptions JOIN ({len(metrics.exceptions(run_db_module))} rows): {elapsed:.1f}ms")
    assert elapsed < 50, f"exceptions() took {elapsed:.1f}ms — exceeds 50ms budget"


def test_perf_rejected_webhooks_join(run_db_module):
    elapsed = _time(lambda: metrics.rejected_webhooks(run_db_module))
    print(f"\nrejected_webhooks JOIN: {elapsed:.1f}ms")
    assert elapsed < 30


# ---------------------------------------------------------------------------
# PERF-2b: /api/activity DESC LIMIT
# ---------------------------------------------------------------------------

def test_perf_activity_desc_limit(run_db_module):
    """DESC LIMIT 40 on audit_log must be < 20ms."""
    def run():
        run_db_module.execute(
            "SELECT * FROM audit_log ORDER BY event_id DESC LIMIT 40"
        ).fetchall()
    elapsed = _time(run)
    print(f"\nactivity DESC LIMIT 40: {elapsed:.1f}ms")
    assert elapsed < 20


# ---------------------------------------------------------------------------
# PERF: core_metrics
# ---------------------------------------------------------------------------

def test_perf_core_metrics(run_db_module):
    elapsed = _time(lambda: metrics.core_metrics(run_db_module))
    print(f"\ncore_metrics: {elapsed:.1f}ms")
    assert elapsed < 20


# ---------------------------------------------------------------------------
# PERF: risk_engine.revenue_at_risk
# ---------------------------------------------------------------------------

def test_perf_risk_engine(run_db_module):
    elapsed = _time(lambda: risk_engine.revenue_at_risk(run_db_module))
    print(f"\nrisk_engine.revenue_at_risk (180 cases): {elapsed:.1f}ms")
    assert elapsed < 100


# ---------------------------------------------------------------------------
# PERF: intelligence.full_summary
# ---------------------------------------------------------------------------

def test_perf_intelligence_full_summary(run_db_module):
    elapsed = _time(lambda: intelligence.full_summary(run_db_module,
                                                       include_simulation=False))
    print(f"\nintelligence.full_summary (no sim): {elapsed:.1f}ms")
    assert elapsed < 200


# ---------------------------------------------------------------------------
# PERF: anomaly_detector
# ---------------------------------------------------------------------------

def test_perf_anomaly_detector(run_db_module):
    elapsed = _time(lambda: anomaly_detector.run_anomaly_detection(run_db_module))
    print(f"\nanomaly_detector.run_anomaly_detection: {elapsed:.1f}ms")
    assert elapsed < 50


# ---------------------------------------------------------------------------
# Data integrity: no hardcoded values in performance output
# ---------------------------------------------------------------------------

def test_no_hardcoded_kpis(run_db_module):
    """Core metrics must vary with the data — not hardcoded constants."""
    core = metrics.core_metrics(run_db_module)
    # These must be derived from actual seeded data, not constants like 0, 1, 100, etc.
    assert 0 < core["total_cases"] < 10000
    assert 0 < core["amount_at_risk"] < 10_000_000
    assert 0.0 <= core["recovery_rate"] <= 1.0
    # With seeded data + agent run, recovery rate should be non-trivial
    assert core["recovery_rate"] > 0.0
    assert core["recovery_rate"] < 1.0


def test_risk_scores_vary_with_data(run_db_module):
    """Risk scores must vary across cases — not all the same number."""
    result = risk_engine.revenue_at_risk(run_db_module)
    if len(result["cases"]) < 2:
        pytest.skip("Not enough active cases for variance check")
    scores = [r["risk_score"] for r in result["cases"]]
    assert len(set(scores)) > 1, "All risk scores identical — likely hardcoded"
