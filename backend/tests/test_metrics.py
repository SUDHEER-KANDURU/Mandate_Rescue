"""Unit tests for backend/metrics.py.

Uses conftest's fresh_db (180 seeded cases, no agent run) and empty_db fixtures.
Verifies core_metrics exclusions, cohort bucketing, exceptions list, and
rejected_webhooks.
"""

import pytest
import db
import metrics as metrics_module
import agent as agent_module
import seed as seed_module


# ---------------------------------------------------------------------------
# core_metrics — empty DB
# ---------------------------------------------------------------------------

class TestCoreMetricsEmpty:
    def test_all_zeros_on_empty_db(self, empty_db):
        result = metrics_module.core_metrics(empty_db)
        assert result["total_cases"] == 0
        assert result["amount_at_risk"] == 0.0
        assert result["amount_recovered"] == 0.0
        assert result["recovered_cases"] == 0
        assert result["escalated_cases"] == 0
        assert result["recovery_rate"] == 0.0
        assert result["escalation_rate"] == 0.0
        assert result["amount_recovery_rate"] == 0.0


# ---------------------------------------------------------------------------
# core_metrics — fresh seeded DB (no agent run, all 'new')
# ---------------------------------------------------------------------------

class TestCoreMetricsSeeded:
    def test_total_cases_is_180(self, fresh_db):
        result = metrics_module.core_metrics(fresh_db)
        assert result["total_cases"] == 180

    def test_amount_at_risk_positive(self, fresh_db):
        result = metrics_module.core_metrics(fresh_db)
        assert result["amount_at_risk"] > 0

    def test_no_recovered_before_agent_run(self, fresh_db):
        result = metrics_module.core_metrics(fresh_db)
        assert result["recovered_cases"] == 0
        assert result["amount_recovered"] == 0.0
        assert result["recovery_rate"] == 0.0

    def test_rates_are_between_zero_and_one(self, fresh_db):
        result = metrics_module.core_metrics(fresh_db)
        for key in ("recovery_rate", "escalation_rate", "amount_recovery_rate"):
            assert 0.0 <= result[key] <= 1.0, f"{key} out of range"

    def test_required_keys_present(self, fresh_db):
        result = metrics_module.core_metrics(fresh_db)
        for key in ("total_cases", "amount_at_risk", "amount_recovered",
                    "recovered_cases", "escalated_cases", "recovery_rate",
                    "escalation_rate", "amount_recovery_rate"):
            assert key in result


# ---------------------------------------------------------------------------
# core_metrics — invalid and duplicate cases excluded from totals
# ---------------------------------------------------------------------------

class TestCoreMetricsExclusions:
    def test_invalid_cases_excluded_from_total(self, empty_db):
        # Insert one normal case and one invalid case.
        db.insert_mandate_failure(empty_db, {
            "customer_id": "VALID1", "amount": 1000.0,
            "failure_reason": "insufficient_funds", "failure_date": "2024-01-01",
            "past_retry_count": 0, "customer_tenure_months": 6,
            "past_payment_success_rate": 0.8, "merchant_category": "subscription",
            "case_status": "new", "mandate_limit": 5000.0, "source": "synthetic",
        })
        db.insert_mandate_failure(empty_db, {
            "customer_id": "INVALID1", "amount": 500.0,
            "failure_reason": "insufficient_funds", "failure_date": "2024-01-01",
            "past_retry_count": 0, "customer_tenure_months": 3,
            "past_payment_success_rate": 0.5, "merchant_category": "emi",
            "case_status": "invalid", "mandate_limit": 5000.0, "source": "synthetic",
        })
        empty_db.commit()
        result = metrics_module.core_metrics(empty_db)
        # Only the valid case counts
        assert result["total_cases"] == 1
        assert result["amount_at_risk"] == pytest.approx(1000.0)

    def test_duplicate_cases_excluded_from_total(self, empty_db):
        db.insert_mandate_failure(empty_db, {
            "customer_id": "DUP1", "amount": 2000.0,
            "failure_reason": "mandate_expired", "failure_date": "2024-01-01",
            "past_retry_count": 0, "customer_tenure_months": 12,
            "past_payment_success_rate": 0.9, "merchant_category": "insurance",
            "case_status": "duplicate", "mandate_limit": 5000.0, "source": "synthetic",
        })
        empty_db.commit()
        result = metrics_module.core_metrics(empty_db)
        assert result["total_cases"] == 0

    def test_rejected_cases_included_in_total(self, empty_db):
        # 'rejected' (bad signature) IS included in the denominator.
        db.insert_mandate_failure(empty_db, {
            "customer_id": "REJ1", "amount": 3000.0,
            "failure_reason": "bank_technical_error", "failure_date": "2024-01-01",
            "past_retry_count": 0, "customer_tenure_months": 6,
            "past_payment_success_rate": 0.7, "merchant_category": "utility",
            "case_status": "rejected", "mandate_limit": 5000.0, "source": "synthetic",
        })
        empty_db.commit()
        result = metrics_module.core_metrics(empty_db)
        assert result["total_cases"] == 1


# ---------------------------------------------------------------------------
# cohorts
# ---------------------------------------------------------------------------

class TestCohorts:
    def test_returns_by_tenure_and_by_category(self, fresh_db):
        result = metrics_module.cohorts(fresh_db)
        assert "by_tenure" in result
        assert "by_category" in result

    def test_by_tenure_segments_are_known_buckets(self, fresh_db):
        result = metrics_module.cohorts(fresh_db)
        valid = set(metrics_module.TENURE_BUCKET_ORDER)
        for row in result["by_tenure"]:
            assert row["segment"] in valid

    def test_by_category_segments_are_strings(self, fresh_db):
        result = metrics_module.cohorts(fresh_db)
        for row in result["by_category"]:
            assert isinstance(row["segment"], str)

    def test_cohort_rows_have_required_keys(self, fresh_db):
        result = metrics_module.cohorts(fresh_db)
        required = {"segment", "total", "recovered", "recovery_rate",
                    "amount_at_risk", "amount_recovered"}
        for row in result["by_tenure"] + result["by_category"]:
            assert required <= set(row.keys())

    def test_cohort_recovery_rates_in_range(self, fresh_db):
        result = metrics_module.cohorts(fresh_db)
        for row in result["by_tenure"] + result["by_category"]:
            assert 0.0 <= row["recovery_rate"] <= 1.0

    def test_cohort_totals_sum_to_180(self, fresh_db):
        result = metrics_module.cohorts(fresh_db)
        assert sum(r["total"] for r in result["by_tenure"]) == 180
        assert sum(r["total"] for r in result["by_category"]) == 180


# ---------------------------------------------------------------------------
# exceptions
# ---------------------------------------------------------------------------

class TestExceptions:
    def test_empty_before_agent_run(self, fresh_db):
        # All cases are 'new', so no exceptions yet.
        result = metrics_module.exceptions(fresh_db)
        assert result == []

    def test_escalated_cases_appear_after_run(self, fresh_db):
        agent_module.run_agent(conn=fresh_db)
        fresh_db.commit()
        result = metrics_module.exceptions(fresh_db)
        # After a run there should be escalated cases (mandate_revoked + others)
        assert len(result) > 0
        for row in result:
            assert row["case_status"] in metrics_module.EXCEPTION_STATUSES

    def test_exception_rows_have_required_keys(self, fresh_db):
        agent_module.run_agent(conn=fresh_db)
        fresh_db.commit()
        result = metrics_module.exceptions(fresh_db)
        required = {"customer_id", "amount", "failure_reason", "merchant_category",
                    "case_status", "last_action", "why_unrecovered"}
        for row in result:
            assert required <= set(row.keys())

    def test_exceptions_sorted_by_amount_desc(self, fresh_db):
        agent_module.run_agent(conn=fresh_db)
        fresh_db.commit()
        result = metrics_module.exceptions(fresh_db)
        amounts = [r["amount"] for r in result]
        assert amounts == sorted(amounts, reverse=True)


# ---------------------------------------------------------------------------
# rejected_webhooks
# ---------------------------------------------------------------------------

class TestRejectedWebhooks:
    def test_empty_before_agent_run(self, fresh_db):
        # No audit rows yet.
        result = metrics_module.rejected_webhooks(fresh_db)
        assert result == []

    def test_rejected_cases_appear_after_run(self, fresh_db):
        agent_module.run_agent(conn=fresh_db)
        fresh_db.commit()
        result = metrics_module.rejected_webhooks(fresh_db)
        # Seed plants 3 spoofed signatures (CUST1007, CUST1042, CUST1099).
        assert len(result) == 3

    def test_rejected_webhook_row_keys(self, fresh_db):
        agent_module.run_agent(conn=fresh_db)
        fresh_db.commit()
        result = metrics_module.rejected_webhooks(fresh_db)
        required = {"customer_id", "raw_event_type", "amount",
                    "failure_reason", "event_timestamp", "reason"}
        for row in result:
            assert required <= set(row.keys())
