"""Real pytest wrapper around audit_check.py's 7 correctness rules.

audit_check.run_audit() already re-derives ground truth from the DB and returns a
structured PASS/FAIL report; this file runs a full agent pass over a fresh seeded
database once per test session and asserts every rule passes, with the specific
violating customer_ids surfaced in the assertion message on failure. This turns
"the audit script says it passes" into "pytest verifies it, on every CI run."
"""

import random

import pytest

import agent as agent_module
import audit_check
import db


@pytest.fixture(scope="module")
def audited_conn():
    """One fresh seeded DB, run through the full agent pipeline once, reused by
    every test in this module (they only read; nothing mutates after the run)."""
    conn = db.get_memory_connection()
    db.init_db(conn)
    import seed as seed_module
    rng = random.Random(seed_module.SEED)
    records = seed_module.build_records(rng)
    for rec in records:
        db.insert_mandate_failure(conn, rec)
    conn.commit()
    agent_module.run_agent(policy=agent_module.PolicyParams(use_llm=False), conn=conn)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def report(audited_conn):
    return audit_check.run_audit(audited_conn)


def _violation_summary(check):
    return "; ".join(
        f"{v.get('customer_id')}: {v['detail']}" for v in check["violations"]
    )


def test_run_completed(report):
    assert report["run_completed"] is True
    assert report["total_cases"] == 180


@pytest.mark.parametrize("rule_id", [
    "rule_1_mandate_revoked_no_retry",
    "rule_2_retry_cap",
    "rule_3_pre_debit_before_retry",
    "rule_4_rejected_isolation",
    "rule_5_over_limit_reauth",
    "rule_6_money_figures_match",
    "rule_7_comparison_sentence",
])
def test_audit_rule_passes(report, rule_id):
    check = next(c for c in report["checks"] if c["id"] == rule_id)
    assert check["passed"], (
        f"{rule_id} ({check['description']}) failed with "
        f"{check['violation_count']} violation(s): {_violation_summary(check)}"
    )


def test_overall_audit_passes(report):
    assert report["passed"], (
        f"{report['total_violations']} total violation(s) across the audit — see "
        f"individual rule test failures for detail."
    )


def test_reproducible_recovered_escalated_counts(audited_conn):
    """A fixed seed must always yield the same status split. Pin the exact numbers
    so a future change that silently alters RNG draw order, retry cap, or scoring
    is caught immediately rather than discovered later.

    NOTE: this is 139 recovered / 38 escalated / 3 rejected (180 total), not the
    142/38 figure in older README text — that number predates the webhook-signature
    -rejection feature (seed.py's 3 deliberately-spoofed SPOOFED_CUSTOMER_IDS are
    always rejected before scoring, so only 177 of the 180 cases ever reach
    recovered/escalated). This test pins the CURRENT, code-verified ground truth;
    the README has been corrected to match (see AUDIT_AND_UPGRADE_PLAN.md).
    """
    cases = db.get_all_cases(audited_conn)
    counts = {}
    for c in cases:
        counts[c["case_status"]] = counts.get(c["case_status"], 0) + 1
    assert counts.get("recovered", 0) == 139, counts
    assert counts.get("escalated", 0) == 38, counts
    assert counts.get("rejected", 0) == 3, counts
