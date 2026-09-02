"""Unit tests for backend/health.py.

Covers score formula, band thresholds, boundary inputs, and health_for_case().
"""

import pytest
import health


# ---------------------------------------------------------------------------
# health_score
# ---------------------------------------------------------------------------

class TestHealthScore:
    def test_perfect_score(self):
        # success_rate=1.0, retry_count=0 → maximum score
        score = health.health_score(1.0, 0)
        assert score == 100

    def test_worst_score(self):
        # success_rate=0.0, retry_count >= RETRY_CAP → minimum score
        score = health.health_score(0.0, health.RETRY_CAP)
        assert score == 0

    def test_score_is_int(self):
        assert isinstance(health.health_score(0.8, 1), int)

    def test_score_in_range(self):
        for sr in [0.0, 0.25, 0.5, 0.75, 1.0]:
            for rc in [0, 1, 2, 3]:
                s = health.health_score(sr, rc)
                assert 0 <= s <= 100, f"out of range: health_score({sr}, {rc}) = {s}"

    def test_higher_success_rate_raises_score(self):
        low  = health.health_score(0.2, 1)
        high = health.health_score(0.9, 1)
        assert high > low

    def test_lower_retry_count_raises_score(self):
        less_retries = health.health_score(0.7, 0)
        more_retries = health.health_score(0.7, 3)
        assert less_retries > more_retries

    def test_success_rate_clamped_below_zero(self):
        normal = health.health_score(0.0, 0)
        clamped = health.health_score(-1.0, 0)
        assert normal == clamped

    def test_success_rate_clamped_above_one(self):
        normal  = health.health_score(1.0, 0)
        clamped = health.health_score(5.0, 0)
        assert normal == clamped

    def test_retry_count_beyond_cap_treated_as_cap(self):
        at_cap    = health.health_score(0.5, health.RETRY_CAP)
        beyond_cap = health.health_score(0.5, health.RETRY_CAP + 10)
        assert at_cap == beyond_cap

    def test_weights_sum_to_one(self):
        assert abs(health.W_SUCCESS + health.W_RETRY - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# health_band
# ---------------------------------------------------------------------------

class TestHealthBand:
    def test_healthy_band(self):
        assert health.health_band(70) == "healthy"
        assert health.health_band(100) == "healthy"
        assert health.health_band(85) == "healthy"

    def test_at_risk_band(self):
        assert health.health_band(45) == "at-risk"
        assert health.health_band(69) == "at-risk"
        assert health.health_band(55) == "at-risk"

    def test_high_risk_band(self):
        assert health.health_band(0) == "high-risk"
        assert health.health_band(44) == "high-risk"
        assert health.health_band(20) == "high-risk"

    @pytest.mark.parametrize("score,expected", [
        (70, "healthy"),
        (69, "at-risk"),
        (45, "at-risk"),
        (44, "high-risk"),
    ])
    def test_band_boundary_exact(self, score, expected):
        assert health.health_band(score) == expected


# ---------------------------------------------------------------------------
# health_for_case
# ---------------------------------------------------------------------------

class TestHealthForCase:
    def _case(self, success_rate=0.9, retry_count=0):
        return {
            "past_payment_success_rate": success_rate,
            "past_retry_count": retry_count,
        }

    def test_returns_required_keys(self):
        result = health.health_for_case(self._case())
        assert "health_score" in result
        assert "health_band" in result
        assert "reasoning" in result

    def test_score_consistent_with_health_score(self):
        case = self._case(success_rate=0.7, retry_count=2)
        result = health.health_for_case(case)
        expected = health.health_score(0.7, 2)
        assert result["health_score"] == expected

    def test_band_consistent_with_score(self):
        case = self._case(success_rate=0.3, retry_count=3)
        result = health.health_for_case(case)
        assert result["health_band"] == health.health_band(result["health_score"])

    def test_reasoning_is_string(self):
        result = health.health_for_case(self._case())
        assert isinstance(result["reasoning"], str)
        assert len(result["reasoning"]) > 0

    def test_reasoning_contains_score(self):
        case = self._case(success_rate=0.8, retry_count=1)
        result = health.health_for_case(case)
        assert str(result["health_score"]) in result["reasoning"]

    def test_missing_fields_do_not_raise(self):
        result = health.health_for_case({})
        assert isinstance(result["health_score"], int)
        assert result["health_band"] in ("healthy", "at-risk", "high-risk")
