"""Unit tests for backend/query.py.

Uses a fresh in-memory DB seeded via conftest. query.run_query() opens and closes
its own connection via db.get_connection(). We patch that function to return a
non-closing proxy around our shared in-memory connection so the test DB survives
between calls.
"""

import pytest
import db
import query as query_module


# ---------------------------------------------------------------------------
# Connection proxy: delegates all attribute access to the real connection but
# ignores close() so the shared in-memory DB survives across run_query calls.
# ---------------------------------------------------------------------------

class _NoCloseProxy:
    """Proxy that forwards everything to the wrapped sqlite3 connection
    except close(), which is silently ignored."""
    def __init__(self, conn):
        object.__setattr__(self, '_conn', conn)

    def close(self):
        pass  # intentionally suppressed

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_conn'), name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


@pytest.fixture
def patched_db(fresh_db, monkeypatch):
    """Make query.run_query() use our isolated in-memory DB."""
    proxy = _NoCloseProxy(fresh_db)
    monkeypatch.setattr(db, "get_connection", lambda: proxy)
    yield fresh_db


# ---------------------------------------------------------------------------
# Basic query — no filters
# ---------------------------------------------------------------------------

class TestNoFilter:
    def test_returns_all_cases(self, patched_db):
        rows, applied = query_module.run_query({})
        # 180 seeded cases; default limit=100
        assert len(rows) == 100
        assert applied == {}

    def test_default_sort_is_descending_by_amount(self, patched_db):
        rows, _ = query_module.run_query({})
        amounts = [r["amount"] for r in rows]
        assert amounts == sorted(amounts, reverse=True)

    def test_each_row_has_score_and_health_band(self, patched_db):
        rows, _ = query_module.run_query({})
        for r in rows:
            assert "score" in r
            assert "health_band" in r
            assert r["health_band"] in ("healthy", "at-risk", "high-risk")

    def test_each_row_has_over_limit_field(self, patched_db):
        rows, _ = query_module.run_query({})
        for r in rows:
            assert "over_limit" in r
            assert isinstance(r["over_limit"], bool)


# ---------------------------------------------------------------------------
# Equality filters
# ---------------------------------------------------------------------------

class TestEqualityFilters:
    def test_filter_by_failure_reason(self, patched_db):
        rows, applied = query_module.run_query({"failure_reason": "insufficient_funds"})
        for r in rows:
            assert r["failure_reason"] == "insufficient_funds"
        assert applied.get("failure_reason") == "insufficient_funds"

    def test_filter_by_case_status_new(self, patched_db):
        rows, applied = query_module.run_query({"case_status": "new"})
        for r in rows:
            assert r["case_status"] == "new"

    def test_filter_by_merchant_category(self, patched_db):
        rows, applied = query_module.run_query({"merchant_category": "emi"})
        for r in rows:
            assert r["merchant_category"] == "emi"

    def test_invalid_value_for_known_field_ignored(self, patched_db):
        rows, applied = query_module.run_query({"failure_reason": "evil_value"})
        assert "failure_reason" not in applied
        assert len(rows) > 0

    def test_unknown_field_ignored(self, patched_db):
        rows, applied = query_module.run_query({"DROP TABLE": "mandate_failures"})
        assert "DROP TABLE" not in applied
        assert len(rows) > 0


# ---------------------------------------------------------------------------
# Numeric range filters
# ---------------------------------------------------------------------------

class TestNumericFilters:
    def test_amount_min_filter(self, patched_db):
        rows, applied = query_module.run_query({"amount_min": 5000})
        for r in rows:
            assert r["amount"] >= 5000
        assert applied.get("amount_min") == 5000.0

    def test_amount_max_filter(self, patched_db):
        rows, applied = query_module.run_query({"amount_max": 1000})
        for r in rows:
            assert r["amount"] <= 1000
        assert applied.get("amount_max") == 1000.0

    def test_amount_range_filter(self, patched_db):
        rows, applied = query_module.run_query({"amount_min": 2000, "amount_max": 4000})
        for r in rows:
            assert 2000 <= r["amount"] <= 4000

    def test_non_numeric_amount_min_ignored(self, patched_db):
        rows, applied = query_module.run_query({"amount_min": "not_a_number"})
        assert "amount_min" not in applied

    def test_dunning_stage_min_filter(self, patched_db):
        # All seeded cases start at dunning_stage=0; this should return empty or all.
        rows, applied = query_module.run_query({"dunning_stage_min": 0})
        assert "dunning_stage_min" in applied


# ---------------------------------------------------------------------------
# Computed filters — score and health_band
# ---------------------------------------------------------------------------

class TestComputedFilters:
    def test_score_min_filter(self, patched_db):
        rows, applied = query_module.run_query({"score_min": 70})
        for r in rows:
            assert r["score"] >= 70
        assert "score_min" in applied

    def test_score_max_filter(self, patched_db):
        rows, applied = query_module.run_query({"score_max": 50})
        for r in rows:
            assert r["score"] <= 50

    def test_score_range(self, patched_db):
        rows, applied = query_module.run_query({"score_min": 40, "score_max": 60})
        for r in rows:
            assert 40 <= r["score"] <= 60

    def test_health_band_filter_healthy(self, patched_db):
        rows, applied = query_module.run_query({"health_band": "healthy"})
        for r in rows:
            assert r["health_band"] == "healthy"
        assert applied.get("health_band") == "healthy"

    def test_invalid_health_band_ignored(self, patched_db):
        rows, applied = query_module.run_query({"health_band": "super-healthy"})
        assert "health_band" not in applied


# ---------------------------------------------------------------------------
# over_limit filter
# ---------------------------------------------------------------------------

class TestOverLimitFilter:
    def test_over_limit_true(self, patched_db):
        rows, applied = query_module.run_query({"over_limit": True})
        for r in rows:
            assert r["over_limit"] is True
        assert applied.get("over_limit") is True

    def test_over_limit_false(self, patched_db):
        rows, applied = query_module.run_query({"over_limit": False})
        for r in rows:
            assert r["over_limit"] is False


# ---------------------------------------------------------------------------
# Sort direction
# ---------------------------------------------------------------------------

class TestSortDirection:
    def test_sort_asc(self, patched_db):
        rows, applied = query_module.run_query({"sort_by_amount": "asc"})
        amounts = [r["amount"] for r in rows]
        assert amounts == sorted(amounts)
        assert applied.get("sort_by_amount") == "asc"

    def test_sort_desc_explicit(self, patched_db):
        rows, applied = query_module.run_query({"sort_by_amount": "desc"})
        amounts = [r["amount"] for r in rows]
        assert amounts == sorted(amounts, reverse=True)

    def test_unknown_sort_value_defaults_to_desc(self, patched_db):
        rows, _ = query_module.run_query({"sort_by_amount": "sideways"})
        amounts = [r["amount"] for r in rows]
        assert amounts == sorted(amounts, reverse=True)


# ---------------------------------------------------------------------------
# Limit
# ---------------------------------------------------------------------------

def test_custom_limit(patched_db):
    rows, _ = query_module.run_query({}, limit=10)
    assert len(rows) <= 10


def test_none_spec_is_safe(patched_db):
    rows, applied = query_module.run_query(None)
    assert isinstance(rows, list)
    assert applied == {}
