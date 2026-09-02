"""Unit and integration tests for the explicit payment state machine.

Tests:
- LEGAL_TRANSITIONS map completeness and correctness
- is_legal_transition() allows and rejects the right edges
- _RunContext.set_status() enforces the machine (raises on illegal)
- State transitions are persisted to state_transitions table
- Terminal states (recovered, escalated, rejected) have no outbound transitions
- A full agent run produces a consistent transition history for every case
"""

import random

import pytest

import db
import agent as agent_module


# ---------------------------------------------------------------------------
# Unit tests for the LEGAL_TRANSITIONS map
# ---------------------------------------------------------------------------

class TestLegalTransitionsMap:
    def test_all_statuses_have_entries(self):
        """Every status that can appear in case_status must be a key."""
        known = {
            "new", "in_progress", "recovered", "escalated",
            "rejected", "invalid", "promised", "broken_promise",
        }
        missing = known - set(db.LEGAL_TRANSITIONS.keys())
        assert not missing, f"Missing from LEGAL_TRANSITIONS: {missing}"

    def test_terminal_states_have_no_outbound(self):
        for status in ("recovered", "escalated", "rejected", "invalid"):
            assert db.LEGAL_TRANSITIONS[status] == frozenset(), (
                f"Terminal status '{status}' should have no outbound transitions"
            )

    def test_new_can_reach_in_progress(self):
        assert db.is_legal_transition("new", "in_progress")

    def test_new_can_reach_rejected(self):
        assert db.is_legal_transition("new", "rejected")

    def test_new_can_reach_invalid(self):
        assert db.is_legal_transition("new", "invalid")

    def test_in_progress_can_reach_recovered(self):
        assert db.is_legal_transition("in_progress", "recovered")

    def test_in_progress_can_reach_escalated(self):
        assert db.is_legal_transition("in_progress", "escalated")

    def test_recovered_cannot_go_back_to_in_progress(self):
        assert not db.is_legal_transition("recovered", "in_progress")

    def test_recovered_cannot_go_back_to_new(self):
        assert not db.is_legal_transition("recovered", "new")

    def test_escalated_cannot_transition_anywhere(self):
        for target in ("recovered", "new", "in_progress", "rejected"):
            assert not db.is_legal_transition("escalated", target), (
                f"escalated -> {target} should be illegal"
            )

    def test_rejected_cannot_transition_anywhere(self):
        for target in ("recovered", "new", "in_progress", "escalated"):
            assert not db.is_legal_transition("rejected", target)

    def test_invalid_cannot_transition_anywhere(self):
        for target in ("recovered", "new", "in_progress", "escalated"):
            assert not db.is_legal_transition("invalid", target)


# ---------------------------------------------------------------------------
# _RunContext.set_status enforcement
# ---------------------------------------------------------------------------

def _make_context(conn):
    rng = random.Random(42)
    return agent_module._RunContext(conn, rng)


def _make_case(customer_id="CUST_SM_TEST", status="new"):
    return {
        "customer_id": customer_id,
        "amount": 1500.0,
        "failure_reason": "insufficient_funds",
        "failure_date": "2026-01-15",
        "past_retry_count": 0,
        "customer_tenure_months": 12,
        "past_payment_success_rate": 0.8,
        "merchant_category": "subscription",
        "case_status": status,
        "raw_event_type": "payment.failed",
        "mandate_limit": 5000,
        "dunning_stage": 0,
        "source": "synthetic",
    }


def _insert_case(conn, case):
    db.insert_mandate_failure(conn, case)
    conn.commit()


class TestSetStatusEnforcement:
    def test_legal_transition_succeeds(self, empty_db):
        case = _make_case()
        _insert_case(empty_db, case)
        ctx = _make_context(empty_db)
        ctx.set_status(case, "in_progress")
        assert case["case_status"] == "in_progress"
        row = db.get_case(empty_db, "CUST_SM_TEST")
        assert row["case_status"] == "in_progress"

    def test_illegal_transition_raises(self, empty_db):
        case = _make_case(status="recovered")
        _insert_case(empty_db, case)
        ctx = _make_context(empty_db)
        with pytest.raises(ValueError, match="Illegal state transition"):
            ctx.set_status(case, "in_progress")

    def test_no_op_transition_does_not_raise(self, empty_db):
        """Setting status to current status is a no-op, not an error."""
        case = _make_case(status="new")
        _insert_case(empty_db, case)
        ctx = _make_context(empty_db)
        ctx.set_status(case, "new")  # no-op, should not raise
        assert case["case_status"] == "new"

    def test_transition_persisted_to_state_transitions(self, empty_db):
        case = _make_case()
        _insert_case(empty_db, case)
        ctx = _make_context(empty_db)
        ctx.set_status(case, "in_progress")
        empty_db.commit()
        transitions = db.get_state_transitions(empty_db, "CUST_SM_TEST")
        assert len(transitions) == 1
        assert transitions[0]["from_status"] == "new"
        assert transitions[0]["to_status"] == "in_progress"

    def test_multiple_transitions_in_order(self, empty_db):
        case = _make_case()
        _insert_case(empty_db, case)
        ctx = _make_context(empty_db)
        ctx.set_status(case, "in_progress")
        ctx.set_status(case, "recovered")
        empty_db.commit()
        transitions = db.get_state_transitions(empty_db, "CUST_SM_TEST")
        assert len(transitions) == 2
        assert transitions[0]["to_status"] == "in_progress"
        assert transitions[1]["to_status"] == "recovered"

    def test_terminal_to_terminal_raises(self, empty_db):
        case = _make_case(status="escalated")
        _insert_case(empty_db, case)
        ctx = _make_context(empty_db)
        with pytest.raises(ValueError, match="Illegal state transition"):
            ctx.set_status(case, "recovered")


# ---------------------------------------------------------------------------
# Integration: full agent run produces consistent transition history
# ---------------------------------------------------------------------------

def test_full_run_transitions_consistent(fresh_db):
    """After a full agent run, every case that was processed must have:
    - at least one state_transitions row
    - a chain starting from 'new'
    - a final state matching case_status in mandate_failures
    """
    policy = agent_module.PolicyParams(use_llm=False)
    agent_module.run_agent(policy=policy, conn=fresh_db)
    fresh_db.commit()

    cases = db.get_all_cases(fresh_db)
    pipeline_statuses = {"recovered", "escalated", "promised", "broken_promise"}
    processed = [c for c in cases if c["case_status"] in pipeline_statuses]

    # Every processed case should have at least one transition on record.
    for case in processed:
        transitions = db.get_state_transitions(fresh_db, case["customer_id"])
        assert len(transitions) >= 1, (
            f"{case['customer_id']} reached {case['case_status']} with no transitions"
        )
        # The first transition must start from 'new'.
        assert transitions[0]["from_status"] == "new", (
            f"{case['customer_id']} first transition does not start from 'new'"
        )
        # The last transition's to_status must match the current case_status.
        assert transitions[-1]["to_status"] == case["case_status"], (
            f"{case['customer_id']} last transition {transitions[-1]['to_status']} "
            f"!= case_status {case['case_status']}"
        )


def test_no_illegal_transitions_in_full_run(fresh_db):
    """No illegal transition should exist after a full run: every (from, to) pair
    must be in LEGAL_TRANSITIONS."""
    policy = agent_module.PolicyParams(use_llm=False)
    agent_module.run_agent(policy=policy, conn=fresh_db)
    fresh_db.commit()

    rows = fresh_db.execute("SELECT * FROM state_transitions").fetchall()
    violations = []
    for row in rows:
        if not db.is_legal_transition(row["from_status"], row["to_status"]):
            violations.append(dict(row))
    assert not violations, f"Illegal transitions found: {violations}"
