"""Unit tests for backend/salary_window.py.

Covers both adaptive and generic_only modes, history parsing edge cases,
the MIN_HISTORY_POINTS threshold, modal-day inference, and window clamping.
"""

import pytest
import salary_window


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _case(history=""):
    return {"history_success_days": history}


# ---------------------------------------------------------------------------
# generic_only mode
# ---------------------------------------------------------------------------

class TestGenericOnlyMode:
    def test_always_returns_generic(self):
        # Even with rich history, generic_only ignores it.
        case = _case("5,5,5,5")
        result = salary_window.infer_window(case, mode="generic_only")
        assert result["inferred"] is False
        assert result["label"] == "generic"

    def test_target_day_is_first_generic_window_start(self):
        result = salary_window.infer_window(_case(), mode="generic_only")
        assert result["target_day"] == salary_window.GENERIC_WINDOWS[0][0]

    def test_window_matches_first_generic_window(self):
        result = salary_window.infer_window(_case(), mode="generic_only")
        assert result["window"] == salary_window.GENERIC_WINDOWS[0]

    def test_reason_mentions_generic_only(self):
        result = salary_window.infer_window(_case("1,2,3,4"), mode="generic_only")
        assert "generic_only" in result["reason"]


# ---------------------------------------------------------------------------
# Adaptive mode — insufficient history (falls back to generic)
# ---------------------------------------------------------------------------

class TestAdaptiveFallback:
    def test_empty_history_is_generic(self):
        result = salary_window.infer_window(_case(""), mode="adaptive")
        assert result["inferred"] is False

    def test_none_history_is_generic(self):
        result = salary_window.infer_window({}, mode="adaptive")
        assert result["inferred"] is False

    def test_below_threshold_is_generic(self):
        # MIN_HISTORY_POINTS - 1 entries → generic
        entries = ",".join(["10"] * (salary_window.MIN_HISTORY_POINTS - 1))
        result = salary_window.infer_window(_case(entries), mode="adaptive")
        assert result["inferred"] is False

    def test_exactly_threshold_is_inferred(self):
        entries = ",".join(["15"] * salary_window.MIN_HISTORY_POINTS)
        result = salary_window.infer_window(_case(entries), mode="adaptive")
        assert result["inferred"] is True

    def test_generic_fallback_target_day(self):
        result = salary_window.infer_window(_case("1"), mode="adaptive")
        assert result["target_day"] == salary_window.GENERIC_WINDOWS[0][0]


# ---------------------------------------------------------------------------
# Adaptive mode — sufficient history (personalizes)
# ---------------------------------------------------------------------------

class TestAdaptivePersonalized:
    def test_modal_day_detected(self):
        # modal day = 10 (appears 4×), not 5 (appears 1×)
        history = "10,10,10,10,5"
        result = salary_window.infer_window(_case(history), mode="adaptive")
        assert result["inferred"] is True
        assert result["target_day"] == 10

    def test_window_around_modal_day(self):
        history = "15,15,15"
        result = salary_window.infer_window(_case(history), mode="adaptive")
        low, high = result["window"]
        assert low == 14
        assert high == 16

    def test_window_clamped_at_day_1(self):
        # Modal day = 1 → low should be clamped to 1, not 0.
        history = "1,1,1"
        result = salary_window.infer_window(_case(history), mode="adaptive")
        low, _ = result["window"]
        assert low >= 1

    def test_window_clamped_at_day_31(self):
        # Modal day = 31 → high should be clamped to 31, not 32.
        history = "31,31,31"
        result = salary_window.infer_window(_case(history), mode="adaptive")
        _, high = result["window"]
        assert high <= 31

    def test_label_contains_inferred(self):
        history = "20,20,20"
        result = salary_window.infer_window(_case(history), mode="adaptive")
        assert "inferred" in result["label"]

    def test_reason_mentions_history_count(self):
        history = "7,7,7,7"
        result = salary_window.infer_window(_case(history), mode="adaptive")
        assert "4" in result["reason"]


# ---------------------------------------------------------------------------
# History parsing edge cases
# ---------------------------------------------------------------------------

class TestHistoryParsing:
    def test_non_digit_parts_ignored(self):
        # "abc" should be silently dropped; only digits count.
        history = "10,abc,10,10"
        result = salary_window.infer_window(_case(history), mode="adaptive")
        assert result["inferred"] is True  # 3 valid digits ≥ MIN_HISTORY_POINTS
        assert result["target_day"] == 10

    def test_whitespace_around_entries_ignored(self):
        history = " 12 , 12 , 12 "
        result = salary_window.infer_window(_case(history), mode="adaptive")
        assert result["inferred"] is True
        assert result["target_day"] == 12

    def test_empty_parts_skipped(self):
        history = ",,10,10,10,,"
        result = salary_window.infer_window(_case(history), mode="adaptive")
        assert result["inferred"] is True

    def test_single_entry_below_threshold(self):
        # MIN_HISTORY_POINTS is 3; a single entry should not personalize.
        result = salary_window.infer_window(_case("5"), mode="adaptive")
        assert result["inferred"] is False


# ---------------------------------------------------------------------------
# Default mode (no argument) is adaptive
# ---------------------------------------------------------------------------

def test_default_mode_is_adaptive():
    history = "8,8,8"
    result_default  = salary_window.infer_window(_case(history))
    result_adaptive = salary_window.infer_window(_case(history), mode="adaptive")
    assert result_default["inferred"] == result_adaptive["inferred"]
    assert result_default["target_day"] == result_adaptive["target_day"]
