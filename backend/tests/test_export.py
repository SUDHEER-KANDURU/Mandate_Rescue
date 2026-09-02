"""Unit tests for backend/export.py.

Verifies CSV structure, required sections, row counts, and that the
export uses real data (not hardcoded values) by checking DB-derived
figures appear in the output.
"""

import csv
import io
import random
import pytest

import db
import seed as seed_module
import agent as agent_module
import metrics as metrics_module
import export as export_module


# ---------------------------------------------------------------------------
# Connection proxy: ignores close() so the shared in-memory DB survives.
# ---------------------------------------------------------------------------

class _NoCloseProxy:
    def __init__(self, conn):
        object.__setattr__(self, '_conn', conn)

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_conn'), name)


# ---------------------------------------------------------------------------
# Module-scoped fixture: one seeded + agent-run DB for all export tests.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def run_db():
    conn = db.get_memory_connection()
    db.init_db(conn)
    rng = random.Random(seed_module.SEED)
    for rec in seed_module.build_records(rng):
        db.insert_mandate_failure(conn, rec)
    conn.commit()
    agent_module.run_agent(conn=conn)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def csv_text(run_db, monkeypatch):
    proxy = _NoCloseProxy(run_db)
    monkeypatch.setattr(db, "get_connection", lambda: proxy)
    return export_module.build_summary_csv()


@pytest.fixture
def csv_rows(csv_text):
    return list(csv.reader(io.StringIO(csv_text)))


def _flat(csv_rows):
    return "\n".join(",".join(r) for r in csv_rows)


# ---------------------------------------------------------------------------
# CSV is non-empty and well-formed
# ---------------------------------------------------------------------------

def test_csv_is_non_empty(csv_text):
    assert len(csv_text.strip()) > 0


def test_csv_is_parseable(csv_text):
    rows = list(csv.reader(io.StringIO(csv_text)))
    assert len(rows) > 0


# ---------------------------------------------------------------------------
# Required section headers present
# ---------------------------------------------------------------------------

class TestSectionHeaders:
    def test_title_row_present(self, csv_rows):
        assert "Mandate Rescue" in _flat(csv_rows)

    def test_key_metrics_section(self, csv_rows):
        assert "Key Metrics" in _flat(csv_rows)

    def test_agent_vs_baselines_section(self, csv_rows):
        flat = _flat(csv_rows)
        assert "Baselines" in flat or "baselines" in flat.lower()

    def test_tenure_cohort_section(self, csv_rows):
        assert "Tenure" in _flat(csv_rows)

    def test_category_cohort_section(self, csv_rows):
        flat = _flat(csv_rows)
        assert "Category" in flat or "category" in flat.lower()

    def test_exceptions_section(self, csv_rows):
        flat = _flat(csv_rows)
        assert "Exceptions" in flat or "exceptions" in flat.lower()


# ---------------------------------------------------------------------------
# Key metrics values appear in the CSV
# ---------------------------------------------------------------------------

class TestMetricValues:
    def test_total_cases_appears(self, csv_rows, run_db):
        core = metrics_module.core_metrics(run_db)
        assert str(core["total_cases"]) in _flat(csv_rows)

    def test_amount_recovered_appears(self, csv_rows, run_db):
        core = metrics_module.core_metrics(run_db)
        assert str(int(core["amount_recovered"])) in _flat(csv_rows)

    def test_recovery_rate_appears_as_percentage(self, csv_rows, run_db):
        core = metrics_module.core_metrics(run_db)
        pct_str = f"{round(core['recovery_rate'] * 100, 1)}%"
        assert pct_str in _flat(csv_rows)


# ---------------------------------------------------------------------------
# Exceptions section lists unrecovered cases
# ---------------------------------------------------------------------------

class TestExceptionsSection:
    def test_exception_customer_ids_appear(self, csv_rows, run_db):
        exceptions = metrics_module.exceptions(run_db)
        flat = _flat(csv_rows)
        assert len(exceptions) > 0
        found = sum(1 for e in exceptions if e["customer_id"] in flat)
        assert found > 0

    def test_exceptions_column_header_present(self, csv_rows):
        flat = _flat(csv_rows)
        assert "Customer" in flat or "customer" in flat.lower()


# ---------------------------------------------------------------------------
# Baseline comparison rows present
# ---------------------------------------------------------------------------

def test_naive_baseline_row_present(csv_rows):
    flat = _flat(csv_rows)
    assert "naive" in flat.lower() or "Naive" in flat or "1 attempt" in flat.lower()


def test_dumb_persistence_row_present(csv_rows):
    flat = _flat(csv_rows)
    assert "persistence" in flat.lower() or "Dumb" in flat
