"""Unit tests for backend/simulation_runner.py.

Covers:
- _summarize(): CI math, mean, std, edge cases (n=0, n=1)
- _critical_value(): t vs normal fallback behaviour
- _diff_summary(): paired delta CI
- run_monte_carlo(): shape, policy metadata, LLM forced off
- compare_policies(): delta sign, raw arrays stripped
- scipy fallback: the normal approximation path is exercised even when
  scipy is installed by temporarily removing it from the module.
"""

import math
import sys
import types
import pytest

import simulation_runner as sr
import agent as agent_module


# ---------------------------------------------------------------------------
# _summarize — CI math
# ---------------------------------------------------------------------------

class TestSummarize:
    def test_empty_returns_zeros(self):
        s = sr._summarize([])
        assert s["n"] == 0
        assert s["mean"] == 0.0
        assert s["std"] == 0.0
        assert s["ci_low"] == 0.0
        assert s["ci_high"] == 0.0
        assert s["ci_margin"] == 0.0

    def test_single_sample(self):
        s = sr._summarize([0.75])
        assert s["n"] == 1
        assert s["mean"] == pytest.approx(0.75)
        assert s["ci_margin"] == 0.0
        assert s["ci_low"] == pytest.approx(0.75)
        assert s["ci_high"] == pytest.approx(0.75)

    def test_mean_is_correct(self):
        samples = [0.4, 0.6, 0.8]
        s = sr._summarize(samples)
        assert s["mean"] == pytest.approx(0.6, abs=1e-9)

    def test_std_is_sample_std(self):
        # For [0, 1]: sample std = sqrt(((0-0.5)^2 + (1-0.5)^2) / 1) = 0.7071...
        samples = [0.0, 1.0]
        s = sr._summarize(samples)
        assert s["std"] == pytest.approx(math.sqrt(0.5), abs=1e-9)

    def test_ci_interval_contains_mean(self):
        samples = [0.5, 0.6, 0.55, 0.58, 0.52]
        s = sr._summarize(samples)
        assert s["ci_low"] <= s["mean"] <= s["ci_high"]

    def test_ci_symmetric_around_mean(self):
        samples = [0.5, 0.6, 0.55, 0.58, 0.52]
        s = sr._summarize(samples)
        assert s["ci_margin"] == pytest.approx(s["mean"] - s["ci_low"], abs=1e-9)
        assert s["ci_margin"] == pytest.approx(s["ci_high"] - s["mean"], abs=1e-9)

    def test_larger_n_gives_tighter_ci(self):
        import random
        rng = random.Random(42)
        small  = [rng.gauss(0.5, 0.1) for _ in range(5)]
        big    = [rng.gauss(0.5, 0.1) for _ in range(100)]
        s_small = sr._summarize(small)
        s_big   = sr._summarize(big)
        assert s_big["ci_margin"] < s_small["ci_margin"]

    def test_n_key_matches_input_length(self):
        samples = [0.1, 0.2, 0.3, 0.4, 0.5]
        s = sr._summarize(samples)
        assert s["n"] == 5

    def test_all_same_values_gives_zero_margin(self):
        samples = [0.7] * 10
        s = sr._summarize(samples)
        assert s["ci_margin"] == pytest.approx(0.0, abs=1e-9)
        assert s["std"] == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# _critical_value — scipy vs normal fallback
# ---------------------------------------------------------------------------

class TestCriticalValue:
    def test_returns_positive_value(self):
        cv = sr._critical_value(30)
        assert cv > 0

    def test_single_sample_returns_zero(self):
        assert sr._critical_value(1) == 0.0

    def test_large_n_approaches_196(self):
        # For large df, t-distribution approaches standard normal (~1.96 for 95%).
        cv = sr._critical_value(10000)
        assert abs(cv - 1.96) < 0.01

    def test_normal_fallback_returns_196(self):
        """Force the normal approximation path by temporarily hiding scipy."""
        original = sr._HAVE_SCIPY
        original_scipy = sr._scipy_stats
        try:
            sr._HAVE_SCIPY = False
            sr._scipy_stats = None
            cv = sr._critical_value(30, confidence=0.95)
            assert cv == pytest.approx(1.959963984540054, abs=1e-9)
        finally:
            sr._HAVE_SCIPY = original
            sr._scipy_stats = original_scipy

    def test_t_value_larger_than_normal_for_small_n(self):
        """t-critical for df=4 (n=5) should be larger than z=1.96."""
        if not sr._HAVE_SCIPY:
            pytest.skip("scipy not installed")
        cv = sr._critical_value(5)
        assert cv > 1.96


# ---------------------------------------------------------------------------
# _diff_summary — paired delta
# ---------------------------------------------------------------------------

class TestDiffSummary:
    def test_zero_delta_gives_zero_mean(self):
        samples = [0.5, 0.6, 0.55]
        delta = sr._diff_summary(samples, samples)
        assert delta["mean"] == pytest.approx(0.0, abs=1e-9)

    def test_positive_delta(self):
        mod  = [0.7, 0.75, 0.72]
        base = [0.5, 0.55, 0.52]
        delta = sr._diff_summary(mod, base)
        assert delta["mean"] > 0

    def test_negative_delta(self):
        mod  = [0.3, 0.35, 0.32]
        base = [0.5, 0.55, 0.52]
        delta = sr._diff_summary(mod, base)
        assert delta["mean"] < 0

    def test_handles_mismatched_lengths(self):
        # Should use the shorter length without raising.
        mod  = [0.6, 0.7, 0.65, 0.68]
        base = [0.5, 0.55]
        delta = sr._diff_summary(mod, base)
        assert delta["n"] == 2


# ---------------------------------------------------------------------------
# run_monte_carlo — integration (small n for speed)
# ---------------------------------------------------------------------------

class TestRunMonteCarlo:
    def test_returns_required_keys(self):
        result = sr.run_monte_carlo(n_runs=3)
        for key in ("n_runs", "confidence", "policy", "metrics", "used_llm",
                    "critical_method", "raw"):
            assert key in result, f"missing key: {key}"

    def test_llm_always_off(self):
        result = sr.run_monte_carlo(n_runs=3)
        assert result["used_llm"] is False

    def test_n_runs_respected(self):
        result = sr.run_monte_carlo(n_runs=4)
        assert result["n_runs"] == 4
        for key in sr.METRIC_KEYS:
            assert result["raw"][key].__len__() == 4

    def test_metric_keys_present(self):
        result = sr.run_monte_carlo(n_runs=3)
        for key in sr.METRIC_KEYS:
            assert key in result["metrics"]

    def test_recovery_rate_in_range(self):
        result = sr.run_monte_carlo(n_runs=3)
        rr = result["metrics"]["recovery_rate"]
        assert 0.0 <= rr["mean"] <= 1.0

    def test_policy_metadata_correct(self):
        result = sr.run_monte_carlo(n_runs=3)
        p = result["policy"]
        assert "retry_cap" in p
        assert "score_weights" in p
        assert "salary_window_mode" in p
        assert "is_default" in p

    def test_default_policy_is_marked_as_default(self):
        result = sr.run_monte_carlo(n_runs=3)
        assert result["policy"]["is_default"] is True

    def test_modified_policy_is_not_default(self):
        policy = agent_module.PolicyParams(retry_cap=1)
        result = sr.run_monte_carlo(n_runs=3, policy_params=policy)
        assert result["policy"]["is_default"] is False

    def test_raw_arrays_stripped_by_default(self):
        # raw is included in run_monte_carlo but stripped in compare_policies output
        result = sr.run_monte_carlo(n_runs=3)
        assert "raw" in result  # run_monte_carlo DOES include raw

    def test_critical_method_is_string(self):
        result = sr.run_monte_carlo(n_runs=3)
        assert result["critical_method"] in ("t", "normal")


# ---------------------------------------------------------------------------
# compare_policies — delta shape and raw stripped
# ---------------------------------------------------------------------------

class TestComparePolicies:
    def test_returns_required_keys(self):
        policy = agent_module.PolicyParams(retry_cap=2)
        result = sr.compare_policies(policy, n_runs=3)
        for key in ("n_runs", "confidence", "default", "modified", "delta"):
            assert key in result

    def test_raw_arrays_not_in_output(self):
        policy = agent_module.PolicyParams(retry_cap=2)
        result = sr.compare_policies(policy, n_runs=3)
        assert "raw" not in result["default"]
        assert "raw" not in result["modified"]

    def test_delta_has_all_metric_keys(self):
        policy = agent_module.PolicyParams(retry_cap=2)
        result = sr.compare_policies(policy, n_runs=3)
        for key in sr.METRIC_KEYS:
            assert key in result["delta"]

    def test_default_vs_default_delta_near_zero(self):
        """Comparing default policy to itself should give delta ≈ 0."""
        default = agent_module.DEFAULT_POLICY
        result = sr.compare_policies(default, n_runs=5)
        rr_delta = result["delta"]["recovery_rate"]["mean"]
        assert abs(rr_delta) < 0.01  # same seeds → identical runs → zero delta

    def test_n_runs_propagated(self):
        policy = agent_module.PolicyParams(retry_cap=2)
        result = sr.compare_policies(policy, n_runs=4)
        assert result["n_runs"] == 4
