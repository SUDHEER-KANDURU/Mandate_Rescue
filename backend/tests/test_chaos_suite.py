"""Real pytest wrapper around chaos_test.py's 7 adversarial scenarios.

Each underlying scenario function already returns a list of violations (empty =
pass) against its own fresh, isolated in-memory database. This file turns each
scenario into its own pytest test so a CI run reports exactly which specific attack
regressed, rather than one opaque pass/fail for the whole suite.
"""

import pytest

import chaos_test


def _fmt(violations):
    return "; ".join(f"{v.get('customer_id')}: {v['detail']}" for v in violations)


def test_replayed_webhooks_deduplicated():
    v = chaos_test.scenario_replayed_webhooks()
    assert not v, _fmt(v)


def test_invalid_amounts_rejected():
    v = chaos_test.scenario_invalid_amounts()
    assert not v, _fmt(v)


def test_duplicate_customer_ids_handled():
    v = chaos_test.scenario_duplicate_customer_ids()
    assert not v, _fmt(v)


def test_clock_skew_flagged_noncompliant():
    v = chaos_test.scenario_clock_skew()
    assert not v, _fmt(v)


def test_malformed_llm_responses_degrade_gracefully():
    v = chaos_test.scenario_malformed_llm()
    assert not v, _fmt(v)


def test_signature_edge_cases_rejected():
    v = chaos_test.scenario_signature_edge_cases()
    assert not v, _fmt(v)


@pytest.mark.slow
def test_extreme_volume_processes_cleanly():
    v = chaos_test.scenario_extreme_volume(volume=500)  # smaller than the 2000 CLI
    # default so the full pytest run stays fast; the CLI script still exercises 2000.
    assert not v, _fmt(v)


def test_full_chaos_suite_report_passes():
    report = chaos_test.run_chaos_suite()
    assert report["passed"], (
        f"{report['total_failures']} failure(s) across "
        f"{sum(1 for s in report['scenarios'] if not s['passed'])} scenario(s)"
    )
