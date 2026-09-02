"""Unit tests for backend/scoring.py.

Verifies the score formula, weight normalization, boundary conditions,
per-reason base values, and the explain_score text generator.
"""

import pytest
import scoring


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _case(
    success_rate=0.8,
    tenure=12,
    retry_count=0,
    reason="bank_technical_error",
    merchant_category="subscription",
):
    return {
        "past_payment_success_rate": success_rate,
        "customer_tenure_months": tenure,
        "past_retry_count": retry_count,
        "failure_reason": reason,
        "merchant_category": merchant_category,
    }


# ---------------------------------------------------------------------------
# Basic formula correctness
# ---------------------------------------------------------------------------

def test_score_is_int_in_range():
    score, _ = scoring.score_case(_case())
    assert isinstance(score, int)
    assert 0 <= score <= 100


def test_perfect_case_scores_high():
    """All signals maximized -> score near 100."""
    case = _case(success_rate=1.0, tenure=24, retry_count=0, reason="bank_technical_error")
    score, _ = scoring.score_case(case)
    assert score >= 90


def test_worst_case_scores_low():
    """All signals minimized -> score near 0."""
    case = _case(success_rate=0.0, tenure=0, retry_count=3, reason="mandate_revoked")
    score, _ = scoring.score_case(case)
    assert score <= 15


def test_factors_keys():
    _, factors = scoring.score_case(_case())
    assert set(factors.keys()) == {"success_rate", "tenure_component", "retry_component", "reason_base"}


def test_factors_values_in_range():
    _, factors = scoring.score_case(_case())
    for k, v in factors.items():
        assert 0.0 <= v <= 1.0, f"{k}={v} out of [0, 1]"


# ---------------------------------------------------------------------------
# Per-reason REASON_BASE values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason,expected_base", [
    ("bank_technical_error", 0.95),
    ("insufficient_funds",   0.70),
    ("mandate_expired",      0.55),
    ("mandate_revoked",      0.10),
])
def test_reason_base_values(reason, expected_base):
    assert scoring.REASON_BASE[reason] == expected_base


def test_unknown_reason_uses_default():
    """An unrecognised reason should not raise; score stays finite."""
    case = _case(reason="unknown_reason_xyz")
    score, factors = scoring.score_case(case)
    assert isinstance(score, int)
    assert 0 <= score <= 100
    # REASON_BASE.get returns 0.5 for unknown keys (the dict.get default is 0.5)
    assert factors["reason_base"] == 0.5


# ---------------------------------------------------------------------------
# Weight normalization and module-level constants
# ---------------------------------------------------------------------------

def test_default_weights_sum_to_one():
    total = scoring.W_SUCCESS + scoring.W_TENURE + scoring.W_RETRY + scoring.W_REASON
    assert abs(total - 1.0) < 1e-9, f"weights sum to {total}"


def test_custom_weights_respected():
    """Shifting all weight to success_rate should produce a higher score when
    success_rate=1.0 than when success_rate=0.0, regardless of other fields."""
    weights = {"success": 1.0, "tenure": 0.0, "retry": 0.0, "reason": 0.0}
    high, _ = scoring.score_case(_case(success_rate=1.0), weights=weights)
    low, _  = scoring.score_case(_case(success_rate=0.0), weights=weights)
    assert high > low


def test_custom_retry_cap_respected():
    """A case with retry_count=2 should score higher under retry_cap=5 than retry_cap=2."""
    case = _case(retry_count=2)
    score_tight, _ = scoring.score_case(case, retry_cap=2)
    score_loose, _ = scoring.score_case(case, retry_cap=5)
    assert score_loose >= score_tight


# ---------------------------------------------------------------------------
# Boundary / edge inputs
# ---------------------------------------------------------------------------

def test_success_rate_clamped_above_one():
    """success_rate > 1.0 should be treated as 1.0 without raising."""
    score_normal, _ = scoring.score_case(_case(success_rate=1.0))
    score_over,   _ = scoring.score_case(_case(success_rate=2.0))
    assert score_normal == score_over


def test_success_rate_clamped_below_zero():
    """success_rate < 0 should be treated as 0.0 without raising."""
    score_zero, _ = scoring.score_case(_case(success_rate=0.0))
    score_neg,  _ = scoring.score_case(_case(success_rate=-0.5))
    assert score_zero == score_neg


def test_tenure_capped_at_tenure_cap_months():
    """Tenure beyond TENURE_CAP_MONTHS should not push score above 100."""
    score_cap, _ = scoring.score_case(_case(tenure=scoring.TENURE_CAP_MONTHS))
    score_over, _ = scoring.score_case(_case(tenure=scoring.TENURE_CAP_MONTHS + 100))
    assert score_cap == score_over


def test_retry_count_beyond_cap_clamps_to_zero_component():
    """retry_count >= RETRY_CAP should produce retry_component=0.0."""
    _, factors = scoring.score_case(_case(retry_count=scoring.RETRY_CAP))
    assert factors["retry_component"] == 0.0
    _, factors2 = scoring.score_case(_case(retry_count=scoring.RETRY_CAP + 5))
    assert factors2["retry_component"] == 0.0


def test_missing_fields_do_not_raise():
    """score_case must never crash on a minimal / empty case dict."""
    score, factors = scoring.score_case({})
    assert isinstance(score, int)
    assert 0 <= score <= 100


# ---------------------------------------------------------------------------
# explain_score
# ---------------------------------------------------------------------------

def test_explain_score_contains_score_value():
    case = _case(success_rate=0.75, tenure=6, retry_count=1, reason="insufficient_funds")
    score, factors = scoring.score_case(case)
    text = scoring.explain_score(case, score, factors)
    assert str(score) in text


def test_explain_score_contains_failure_reason():
    case = _case(reason="mandate_expired")
    score, factors = scoring.score_case(case)
    text = scoring.explain_score(case, score, factors)
    assert "mandate_expired" in text


def test_explain_score_returns_string():
    case = _case()
    score, factors = scoring.score_case(case)
    assert isinstance(scoring.explain_score(case, score, factors), str)
