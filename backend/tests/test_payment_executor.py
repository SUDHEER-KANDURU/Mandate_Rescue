"""Tests for payment_executor.py — Phase 4.

Covers:
- ExecutionMode / ExecutionOutcome enum values
- ExecutionResult.to_dict() / to_audit_text() serialisation
- PaymentExecutionService.execute_recovery() simulation path (no HTTP)
- PaymentExecutionService.execute_recovery() real path with mocked Razorpay client
- CONFIGURATION_ERROR when credentials absent
- Every real-path outcome: payment captured, already captured (idempotent),
  payment link sent, subscription inactive, invalid resource, API error,
  network error, 401/403 → INVALID_CREDENTIALS
- No fake success: a failed API call must never produce success=True
- verify_razorpay_credentials() structure
"""

import random
import sys
import os
import types
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from payment_executor import (
    ExecutionMode,
    ExecutionOutcome,
    ExecutionResult,
    PaymentExecutionService,
    verify_razorpay_credentials,
    get_executor,
)
import razorpay_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _case(failure_reason="insufficient_funds", amount=2000.0,
          source="synthetic", sub_id=None, pay_id=None):
    c = {
        "customer_id": "TEST001",
        "amount": amount,
        "failure_reason": failure_reason,
        "source": source,
        "mandate_limit": 5000.0,
        "past_payment_success_rate": 0.8,
        "past_retry_count": 0,
        "customer_tenure_months": 12,
        "merchant_category": "subscription",
    }
    if sub_id:
        c["razorpay_subscription_id"] = sub_id
    if pay_id:
        c["razorpay_payment_id"] = pay_id
    return c


# ---------------------------------------------------------------------------
# Enum sanity
# ---------------------------------------------------------------------------

def test_execution_mode_values():
    assert ExecutionMode.REAL_TEST.value == "real_test"
    assert ExecutionMode.SIMULATION.value == "simulation"


def test_execution_outcome_has_no_fake_success():
    """Ensure SIMULATED_SUCCESS is clearly labelled and separate from real outcomes."""
    assert ExecutionOutcome.SIMULATED_SUCCESS.value == "simulated_success"
    assert ExecutionOutcome.PAYMENT_CAPTURED.value == "payment_captured"
    assert ExecutionOutcome.PAYMENT_LINK_SENT.value == "payment_link_sent"


# ---------------------------------------------------------------------------
# ExecutionResult serialisation
# ---------------------------------------------------------------------------

def test_execution_result_to_dict_keys():
    r = ExecutionResult(
        outcome=ExecutionOutcome.SIMULATED_SUCCESS,
        execution_mode=ExecutionMode.SIMULATION,
        success=True,
        amount_rupees=1500.0,
    )
    d = r.to_dict()
    assert d["outcome"] == "simulated_success"
    assert d["execution_mode"] == "simulation"
    assert d["success"] is True
    assert d["amount_rupees"] == 1500.0
    assert "executed_at" in d
    # No secrets in the dict
    assert "key_id" not in d
    assert "key_secret" not in d


def test_execution_result_audit_text_contains_mode():
    r = ExecutionResult(
        outcome=ExecutionOutcome.PAYMENT_LINK_SENT,
        execution_mode=ExecutionMode.REAL_TEST,
        success=True,
        payment_link_url="https://rzp.io/l/test123",
    )
    text = r.to_audit_text()
    assert "Razorpay Test Mode" in text
    assert "payment_link_sent" in text
    assert "https://rzp.io/l/test123" in text


def test_simulation_audit_text_labeled():
    r = ExecutionResult(
        outcome=ExecutionOutcome.SIMULATED_FAILURE,
        execution_mode=ExecutionMode.SIMULATION,
        success=False,
        failure_reason="Simulated failure (prob=0.45)",
    )
    text = r.to_audit_text()
    assert "Simulation" in text
    assert "simulated_failure" in text


# ---------------------------------------------------------------------------
# Simulation path
# ---------------------------------------------------------------------------

def test_simulation_always_uses_rng_not_http():
    """Simulation must never make HTTP calls — verify by checking no razorpay_client
    function is called."""
    rng = random.Random(42)
    svc = PaymentExecutionService(rng=rng)
    case = _case()
    with patch.object(razorpay_client, "_request", side_effect=AssertionError("HTTP called in simulation")):
        result = svc.execute_recovery(case, attempt=1,
                                      execution_mode=ExecutionMode.SIMULATION,
                                      success_prob=1.0)
    assert result.success is True
    assert result.execution_mode == ExecutionMode.SIMULATION
    assert result.outcome == ExecutionOutcome.SIMULATED_SUCCESS


def test_simulation_failure_labeled():
    rng = random.Random(0)
    svc = PaymentExecutionService(rng=rng)
    result = svc.execute_recovery(_case(), attempt=1,
                                  execution_mode=ExecutionMode.SIMULATION,
                                  success_prob=0.0)
    assert result.success is False
    assert result.outcome == ExecutionOutcome.SIMULATED_FAILURE
    # Must contain some indication of synthetic/simulation nature — never invent a success
    reason = result.failure_reason or ""
    assert any(word in reason for word in ("Simulated", "simulation", "synthetic", "benchmark")), \
        f"Expected simulation label in failure_reason, got: {reason!r}"


def test_simulation_never_sets_razorpay_ids():
    rng = random.Random(42)
    svc = PaymentExecutionService(rng=rng)
    result = svc.execute_recovery(_case(), attempt=1,
                                  execution_mode=ExecutionMode.SIMULATION,
                                  success_prob=1.0)
    assert result.razorpay_payment_id is None
    assert result.razorpay_payment_link_id is None
    assert result.payment_link_url is None


# ---------------------------------------------------------------------------
# Real path: CONFIGURATION_ERROR when no credentials
# ---------------------------------------------------------------------------

def test_real_execution_fails_without_credentials():
    """No silent fallback to simulation when credentials are missing."""
    svc = PaymentExecutionService()
    with patch.object(razorpay_client, "credentials_configured", return_value=False):
        result = svc.execute_recovery(_case(source="razorpay_live"), attempt=1,
                                      execution_mode=ExecutionMode.REAL_TEST)
    assert result.success is False
    assert result.outcome == ExecutionOutcome.CONFIGURATION_ERROR
    assert result.execution_mode == ExecutionMode.REAL_TEST
    assert "not configured" in (result.failure_reason or "").lower()


# ---------------------------------------------------------------------------
# Real path: payment capture scenarios
# ---------------------------------------------------------------------------

def test_real_capture_authorized_payment():
    svc = PaymentExecutionService()
    pay_resp = {"id": "pay_test123", "status": "authorized", "amount": 200000}
    captured  = {"id": "pay_test123", "status": "captured", "amount": 200000}
    case = _case(pay_id="pay_test123", amount=2000.0, source="razorpay_live")
    with patch.object(razorpay_client, "credentials_configured", return_value=True), \
         patch.object(razorpay_client, "fetch_payment", return_value=pay_resp), \
         patch.object(razorpay_client, "capture_payment", return_value=captured):
        result = svc.execute_recovery(case, attempt=1,
                                      execution_mode=ExecutionMode.REAL_TEST)
    assert result.success is True
    assert result.outcome == ExecutionOutcome.PAYMENT_CAPTURED
    assert result.razorpay_payment_id == "pay_test123"
    assert result.execution_mode == ExecutionMode.REAL_TEST


def test_real_already_captured_is_idempotent_success():
    svc = PaymentExecutionService()
    pay_resp = {"id": "pay_already", "status": "captured", "amount": 150000}
    case = _case(pay_id="pay_already", amount=1500.0, source="razorpay_live")
    with patch.object(razorpay_client, "credentials_configured", return_value=True), \
         patch.object(razorpay_client, "fetch_payment", return_value=pay_resp):
        result = svc.execute_recovery(case, attempt=1,
                                      execution_mode=ExecutionMode.REAL_TEST)
    assert result.success is True
    assert result.outcome == ExecutionOutcome.PAYMENT_CAPTURED


def test_real_capture_api_error_is_failure():
    svc = PaymentExecutionService()
    pay_resp = {"id": "pay_err", "status": "authorized", "amount": 100000}
    case = _case(pay_id="pay_err", amount=1000.0, source="razorpay_live")
    with patch.object(razorpay_client, "credentials_configured", return_value=True), \
         patch.object(razorpay_client, "fetch_payment", return_value=pay_resp), \
         patch.object(razorpay_client, "capture_payment",
                      side_effect=razorpay_client.RazorpayClientError("500 Internal")):
        result = svc.execute_recovery(case, attempt=1,
                                      execution_mode=ExecutionMode.REAL_TEST)
    assert result.success is False
    assert result.outcome == ExecutionOutcome.API_ERROR


def test_real_payment_not_found_returns_invalid_resource():
    svc = PaymentExecutionService()
    case = _case(pay_id="pay_missing", source="razorpay_live")
    with patch.object(razorpay_client, "credentials_configured", return_value=True), \
         patch.object(razorpay_client, "fetch_payment",
                      side_effect=razorpay_client.RazorpayClientError("404 NOT_FOUND")):
        result = svc.execute_recovery(case, attempt=1,
                                      execution_mode=ExecutionMode.REAL_TEST)
    assert result.success is False
    assert result.outcome == ExecutionOutcome.INVALID_RESOURCE


# ---------------------------------------------------------------------------
# Real path: subscription inactive
# ---------------------------------------------------------------------------

def test_mandate_revoked_returns_subscription_inactive():
    svc = PaymentExecutionService()
    sub_resp = {"id": "sub_rev", "status": "cancelled"}
    case = _case(failure_reason="mandate_revoked", sub_id="sub_rev", source="razorpay_live")
    with patch.object(razorpay_client, "credentials_configured", return_value=True), \
         patch.object(razorpay_client, "fetch_subscription", return_value=sub_resp):
        result = svc.execute_recovery(case, attempt=1,
                                      execution_mode=ExecutionMode.REAL_TEST)
    assert result.success is False
    assert result.outcome == ExecutionOutcome.SUBSCRIPTION_INACTIVE
    assert result.razorpay_subscription_id == "sub_rev"


def test_halted_subscription_returns_inactive():
    svc = PaymentExecutionService()
    sub_resp = {"id": "sub_halt", "status": "halted"}
    case = _case(sub_id="sub_halt", source="razorpay_live")
    with patch.object(razorpay_client, "credentials_configured", return_value=True), \
         patch.object(razorpay_client, "fetch_subscription", return_value=sub_resp), \
         patch.object(razorpay_client, "create_payment_link",
                      side_effect=AssertionError("should not create link for halted sub")):
        result = svc.execute_recovery(case, attempt=1,
                                      execution_mode=ExecutionMode.REAL_TEST)
    assert result.success is False
    assert result.outcome == ExecutionOutcome.SUBSCRIPTION_INACTIVE


# ---------------------------------------------------------------------------
# Real path: payment link creation
# ---------------------------------------------------------------------------

def test_real_creates_payment_link_when_no_pay_id():
    svc = PaymentExecutionService()
    link_resp = {
        "id": "plink_test456",
        "short_url": "https://rzp.io/l/abc123",
        "status": "created",
    }
    case = _case(source="razorpay_live")  # no pay_id, no sub_id
    with patch.object(razorpay_client, "credentials_configured", return_value=True), \
         patch.object(razorpay_client, "create_payment_link", return_value=link_resp):
        result = svc.execute_recovery(case, attempt=1,
                                      execution_mode=ExecutionMode.REAL_TEST)
    assert result.success is True
    assert result.outcome == ExecutionOutcome.PAYMENT_LINK_SENT
    assert result.razorpay_payment_link_id == "plink_test456"
    assert result.payment_link_url == "https://rzp.io/l/abc123"
    assert result.execution_mode == ExecutionMode.REAL_TEST


def test_real_payment_link_401_gives_invalid_credentials():
    svc = PaymentExecutionService()
    case = _case(source="razorpay_live")
    with patch.object(razorpay_client, "credentials_configured", return_value=True), \
         patch.object(razorpay_client, "create_payment_link",
                      side_effect=razorpay_client.RazorpayClientError("401 Unauthorized")):
        result = svc.execute_recovery(case, attempt=1,
                                      execution_mode=ExecutionMode.REAL_TEST)
    assert result.success is False
    assert result.outcome == ExecutionOutcome.INVALID_CREDENTIALS


def test_real_payment_link_network_error():
    svc = PaymentExecutionService()
    case = _case(source="razorpay_live")
    with patch.object(razorpay_client, "credentials_configured", return_value=True), \
         patch.object(razorpay_client, "create_payment_link",
                      side_effect=razorpay_client.RazorpayClientError("network timeout")):
        result = svc.execute_recovery(case, attempt=1,
                                      execution_mode=ExecutionMode.REAL_TEST)
    assert result.success is False
    assert result.outcome == ExecutionOutcome.NETWORK_ERROR


# ---------------------------------------------------------------------------
# Critical invariant: real execution failure must never produce success=True
# ---------------------------------------------------------------------------

def test_any_real_api_error_means_success_false():
    """Exhaustive: try every error type — none must produce success=True."""
    errors = [
        razorpay_client.RazorpayClientError("500 Server Error"),
        razorpay_client.RazorpayClientError("401 Unauthorized"),
        razorpay_client.RazorpayClientError("404 NOT_FOUND"),
        razorpay_client.RazorpayClientError("network timeout"),
        razorpay_client.RazorpayClientError("connection refused"),
    ]
    svc = PaymentExecutionService()
    case = _case(source="razorpay_live")
    for err in errors:
        with patch.object(razorpay_client, "credentials_configured", return_value=True), \
             patch.object(razorpay_client, "create_payment_link", side_effect=err):
            result = svc.execute_recovery(case, attempt=1,
                                          execution_mode=ExecutionMode.REAL_TEST)
        assert result.success is False, \
            f"Error '{err}' produced success=True — this is a fake recovery"


# ---------------------------------------------------------------------------
# verify_razorpay_credentials
# ---------------------------------------------------------------------------

def test_verify_credentials_structure():
    with patch.object(razorpay_client, "_credentials",
                      side_effect=razorpay_client.RazorpayClientError("no key")):
        result = verify_razorpay_credentials()
    assert "configured" in result
    assert "reachable"  in result
    assert "authenticated" in result
    assert "error" in result
    assert result["configured"] is False


def test_verify_credentials_success():
    with patch.object(razorpay_client, "_credentials", return_value=("rzp_test_abc", "secret")), \
         patch.object(razorpay_client, "_request", return_value={"items": []}):
        result = verify_razorpay_credentials()
    assert result["configured"] is True
    assert result["authenticated"] is True
    assert result["mode"] == "test"
    assert result["error"] is None


def test_verify_credentials_bad_key():
    with patch.object(razorpay_client, "_credentials", return_value=("rzp_test_abc", "secret")), \
         patch.object(razorpay_client, "_request",
                      side_effect=razorpay_client.RazorpayClientError("401 Unauthorized")):
        result = verify_razorpay_credentials()
    assert result["configured"] is True
    assert result["authenticated"] is False
    assert result["error"] is not None


# ---------------------------------------------------------------------------
# get_executor singleton
# ---------------------------------------------------------------------------

def test_get_executor_returns_service():
    svc = get_executor()
    assert isinstance(svc, PaymentExecutionService)


def test_get_executor_with_rng_returns_new_instance():
    rng = random.Random(1)
    svc = get_executor(rng=rng)
    assert isinstance(svc, PaymentExecutionService)
    assert svc._rng is rng
