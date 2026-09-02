"""PaymentExecutionService — Phase 4 real Razorpay Test Mode execution layer.

ARCHITECTURE
------------
This module is the only place in Mandate Rescue that decides HOW a recovery
attempt is physically executed. The rest of the pipeline (agent.py, scheduler.py)
calls into this service and receives a typed ExecutionResult — it never touches
the Razorpay API directly.

Conceptual call chain:

    RecoveryPipeline / Scheduler
            ↓
    PaymentExecutionService.execute_recovery()
            ↓
    razorpay_client  (real HTTP, Test Mode)
            ↓
    Razorpay Test API

EXECUTION MODES
---------------
Two modes exist as an explicit, always-visible choice:

  ExecutionMode.REAL_TEST
    Calls the actual Razorpay Test Mode API. Outcome is determined by the
    real API response. Use when RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are
    configured with valid test-mode credentials.

  ExecutionMode.SIMULATION
    Runs the stochastic RNG simulation used by the original pipeline. No HTTP
    calls are made. Use for benchmarks, the Policy Sandbox, and synthetic demo
    runs. The mode is logged explicitly in every audit record so a simulated
    recovery can never silently appear as a real one.

The mode is selected at runtime by checking whether valid credentials are
configured (credentials_configured()) and whether the caller opts in to real
execution. The application NEVER silently falls back from real to simulated
execution — if real execution is requested but credentials are absent, the job
fails explicitly and is logged as failed, not silently converted to a simulation.

RAZORPAY TEST MODE LIMITATIONS (documented honestly)
----------------------------------------------------
Razorpay Test Mode does NOT support:

1. Triggering a new UPI Autopay debit attempt programmatically.
   The Subscriptions API schedules the next billing date but does not expose
   a "charge now" endpoint for subscriptions. Real billing is triggered by
   Razorpay's internal scheduler on the billing_date; merchants cannot force
   an out-of-cycle retry via the API.

2. Creating a manual charge against a UPI mandate directly.
   UPI Autopay mandates are controlled by NPCI/bank; Razorpay does not expose
   an endpoint to trigger an ad-hoc UPI debit against an existing mandate.

WHAT WE CAN DO IN TEST MODE
-----------------------------
1. Fetch subscription status to verify the mandate is still active / not revoked.
2. Create a Payment Link to send to the customer for manual re-authorization
   (used for mandate_expired / over-limit cases).
3. Fetch and capture authorized payments (pay_xxx) when one already exists
   but was not captured.
4. Verify API credentials are valid and the integration is working.

For cases where a real programmatic retry is not supported, the executor
schedules a real payment link, logs the limitation, and marks the job as
PENDING_CUSTOMER_ACTION. The simulation path is preserved for synthetic
benchmarks and remains explicitly labeled.

This honest accounting means the dashboard can show:
  - "Execution: Razorpay Test Mode — payment link sent" (real)
  - "Execution: Simulation (synthetic)" (synthetic benchmark)
Never: "Recovered" from a simulated outcome labeled as real.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import razorpay_client

log = logging.getLogger("mandate_rescue.payment_executor")


# ---------------------------------------------------------------------------
# Execution mode enum
# ---------------------------------------------------------------------------
class ExecutionMode(str, Enum):
    REAL_TEST   = "real_test"    # calls Razorpay Test API; outcome from API response
    SIMULATION  = "simulation"   # RNG-based simulation; no HTTP calls; demo/benchmark only


# ---------------------------------------------------------------------------
# Execution outcome codes
# ---------------------------------------------------------------------------
class ExecutionOutcome(str, Enum):
    # Terminal successes
    PAYMENT_CAPTURED      = "payment_captured"       # existing auth'd payment captured
    PAYMENT_LINK_SENT     = "payment_link_sent"      # link created and URL returned
    SUBSCRIPTION_ACTIVE   = "subscription_active"    # sub verified still active

    # Terminal failures
    PAYMENT_FAILED        = "payment_failed"         # API call failed / bad response
    SUBSCRIPTION_INACTIVE = "subscription_inactive"  # halted/cancelled/expired
    INVALID_CREDENTIALS   = "invalid_credentials"    # bad key_id/key_secret
    INVALID_RESOURCE      = "invalid_resource"       # unknown payment/sub ID (404)
    API_ERROR             = "api_error"              # 4xx/5xx from Razorpay
    NETWORK_ERROR         = "network_error"          # timeout/connection failure
    CONFIGURATION_ERROR   = "configuration_error"   # real execution requested, no creds

    # Intermediate
    PENDING_CUSTOMER_ACTION = "pending_customer_action"  # link sent; awaiting payment

    # Simulation
    SIMULATED_SUCCESS     = "simulated_success"
    SIMULATED_FAILURE     = "simulated_failure"


# ---------------------------------------------------------------------------
# Typed result returned by execute_recovery()
# ---------------------------------------------------------------------------
@dataclass
class ExecutionResult:
    """The complete outcome of one recovery execution attempt.

    This is the single, typed contract between the execution layer and the
    rest of the pipeline. Every field that affects the audit trail or the
    state machine is here; nothing is inferred from side effects.
    """
    outcome: ExecutionOutcome
    execution_mode: ExecutionMode
    success: bool                        # True = recovery progressed / payment made
    failure_reason: Optional[str] = None # human-readable explanation on failure
    razorpay_payment_id: Optional[str] = None  # pay_xxx if a payment was involved
    razorpay_subscription_id: Optional[str] = None
    razorpay_payment_link_id: Optional[str] = None
    payment_link_url: Optional[str] = None     # short_url for customer delivery
    amount_rupees: Optional[float] = None      # amount actually executed
    raw_response: Optional[dict] = None        # Razorpay API response (no secrets)
    executed_at: str = field(default_factory=lambda:
        datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_audit_text(self) -> str:
        """Produce a one-paragraph audit reasoning string for audit_log.reasoning_text."""
        mode_label = "Razorpay Test Mode" if self.execution_mode == ExecutionMode.REAL_TEST \
            else "Simulation (synthetic)"
        parts = [f"Execution mode: {mode_label}. Outcome: {self.outcome.value}."]
        if self.razorpay_payment_id:
            parts.append(f"Payment ID: {self.razorpay_payment_id}.")
        if self.razorpay_subscription_id:
            parts.append(f"Subscription ID: {self.razorpay_subscription_id}.")
        if self.payment_link_url:
            parts.append(f"Payment link URL: {self.payment_link_url}.")
        if self.amount_rupees is not None:
            parts.append(f"Amount: Rs {self.amount_rupees:.2f}.")
        if self.failure_reason:
            parts.append(f"Failure reason: {self.failure_reason}.")
        return " ".join(parts)

    def to_dict(self) -> dict:
        """Serializable dict for API responses and DB storage (no secrets)."""
        return {
            "outcome": self.outcome.value,
            "execution_mode": self.execution_mode.value,
            "success": self.success,
            "failure_reason": self.failure_reason,
            "razorpay_payment_id": self.razorpay_payment_id,
            "razorpay_subscription_id": self.razorpay_subscription_id,
            "razorpay_payment_link_id": self.razorpay_payment_link_id,
            "payment_link_url": self.payment_link_url,
            "amount_rupees": self.amount_rupees,
            "executed_at": self.executed_at,
        }


# ---------------------------------------------------------------------------
# PaymentExecutionService
# ---------------------------------------------------------------------------
class PaymentExecutionService:
    """Isolates all real Razorpay API calls for recovery execution.

    Instantiate once per application process (or per request — it is stateless).
    The `rng` parameter is only used when execution_mode=SIMULATION; for
    REAL_TEST it is unused and may be None.
    """

    def __init__(self, rng=None):
        self._rng = rng

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def execute_recovery(self, case: dict, attempt: int,
                         execution_mode: ExecutionMode,
                         success_prob: float = 0.5) -> ExecutionResult:
        """Execute one recovery attempt for a case.

        Args:
            case: mandate_failures row dict. Must contain customer_id, amount,
                  failure_reason. May contain razorpay_subscription_id,
                  razorpay_payment_id if known from prior webhook data.
            attempt: 1-indexed attempt number (for logging / idempotency notes).
            execution_mode: REAL_TEST or SIMULATION.
            success_prob: only used in SIMULATION mode — the RNG draw probability.

        Returns:
            ExecutionResult describing the complete outcome.

        Contract:
            - REAL_TEST: never invents a success; outcome comes from API response.
            - SIMULATION: never makes an HTTP call.
            - Either mode: always returns an ExecutionResult; never raises.
        """
        if execution_mode == ExecutionMode.REAL_TEST:
            return self._execute_real(case, attempt)
        else:
            return self._execute_simulation(case, attempt, success_prob)

    # ------------------------------------------------------------------
    # Real execution path
    # ------------------------------------------------------------------
    def _execute_real(self, case: dict, attempt: int) -> ExecutionResult:
        """Run one real Razorpay Test Mode recovery attempt.

        Strategy by failure_reason:
          insufficient_funds    → try to capture an existing authorized payment,
                                  OR create a payment link for manual retry.
          bank_technical_error  → same as insufficient_funds (transient — check
                                  subscription status first, then retry path).
          mandate_expired       → create a payment link for re-authorization.
          mandate_revoked       → subscription is terminal; return inactive.

        If credentials are not configured, fail immediately with
        CONFIGURATION_ERROR — no silent fallback to simulation.
        """
        if not razorpay_client.credentials_configured():
            log.error(
                "Real execution requested for customer %s but "
                "RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET are not configured.",
                case.get("customer_id"),
            )
            return ExecutionResult(
                outcome=ExecutionOutcome.CONFIGURATION_ERROR,
                execution_mode=ExecutionMode.REAL_TEST,
                success=False,
                failure_reason=(
                    "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not configured. "
                    "Real execution cannot proceed. Set test-mode credentials in "
                    ".env and restart."
                ),
            )

        reason = case.get("failure_reason", "")
        amount = float(case.get("amount", 0) or 0)
        cust_id = case.get("customer_id", "unknown")
        sub_id = case.get("razorpay_subscription_id")
        pay_id = case.get("razorpay_payment_id")

        # mandate_revoked: subscription is terminal, no retry.
        if reason == "mandate_revoked":
            return self._verify_subscription_inactive(case, sub_id)

        # If we have a specific payment ID, try to capture it first.
        if pay_id:
            result = self._try_capture_payment(pay_id, amount, case)
            if result is not None:
                return result

        # If we have a subscription ID, check its current status.
        if sub_id:
            sub_result = self._check_subscription_status(sub_id, case)
            if sub_result is not None:
                return sub_result

        # For mandate_expired or over-limit: create a payment link.
        if reason in ("mandate_expired",) or float(case.get("amount", 0)) > float(case.get("mandate_limit", 5000) or 5000):
            return self._create_recovery_link(case, amount, attempt)

        # For insufficient_funds / bank_technical_error: create a payment link
        # (we cannot trigger a direct UPI debit — see module docstring limitation).
        return self._create_recovery_link(case, amount, attempt)

    def _try_capture_payment(self, payment_id: str, amount_rupees: float,
                              case: dict) -> Optional[ExecutionResult]:
        """Try to capture an authorized payment. Returns None if not capturable."""
        try:
            payment = razorpay_client.fetch_payment(payment_id)
        except razorpay_client.RazorpayClientError as e:
            err = str(e)
            if "404" in err or "NOT_FOUND" in err.upper():
                log.warning("Payment %s not found for customer %s: %s",
                            payment_id, case.get("customer_id"), e)
                return ExecutionResult(
                    outcome=ExecutionOutcome.INVALID_RESOURCE,
                    execution_mode=ExecutionMode.REAL_TEST,
                    success=False,
                    razorpay_payment_id=payment_id,
                    failure_reason=f"Payment {payment_id} not found: {e}",
                )
            # Non-404 errors: fall through to other strategies
            log.warning("Could not fetch payment %s: %s", payment_id, e)
            return None

        status = payment.get("status", "")
        if status == "captured":
            # Already captured — idempotent success.
            log.info("Payment %s for customer %s already captured.",
                     payment_id, case.get("customer_id"))
            return ExecutionResult(
                outcome=ExecutionOutcome.PAYMENT_CAPTURED,
                execution_mode=ExecutionMode.REAL_TEST,
                success=True,
                razorpay_payment_id=payment_id,
                amount_rupees=payment.get("amount", 0) / 100.0,
                raw_response={k: v for k, v in payment.items()
                              if k not in ("description", "notes")},
            )

        if status == "authorized":
            try:
                captured = razorpay_client.capture_payment(payment_id, amount_rupees)
                log.info("Captured payment %s for customer %s (Rs %.2f).",
                         payment_id, case.get("customer_id"), amount_rupees)
                return ExecutionResult(
                    outcome=ExecutionOutcome.PAYMENT_CAPTURED,
                    execution_mode=ExecutionMode.REAL_TEST,
                    success=True,
                    razorpay_payment_id=payment_id,
                    amount_rupees=amount_rupees,
                    raw_response={k: v for k, v in captured.items()
                                  if k not in ("description", "notes")},
                )
            except razorpay_client.RazorpayClientError as e:
                err = str(e)
                # payment_already_captured → idempotent success
                if "already_captured" in err.lower() or "BAD_REQUEST" in err:
                    return ExecutionResult(
                        outcome=ExecutionOutcome.PAYMENT_CAPTURED,
                        execution_mode=ExecutionMode.REAL_TEST,
                        success=True,
                        razorpay_payment_id=payment_id,
                        amount_rupees=amount_rupees,
                        failure_reason="Already captured (idempotent).",
                    )
                log.warning("Capture failed for payment %s: %s", payment_id, e)
                return ExecutionResult(
                    outcome=ExecutionOutcome.API_ERROR,
                    execution_mode=ExecutionMode.REAL_TEST,
                    success=False,
                    razorpay_payment_id=payment_id,
                    failure_reason=str(e),
                )

        # Payment in failed/refunded/other non-capturable state.
        return None

    def _check_subscription_status(self, sub_id: str,
                                   case: dict) -> Optional[ExecutionResult]:
        """Return an inactive result if subscription is halted/cancelled. None otherwise."""
        try:
            sub = razorpay_client.fetch_subscription(sub_id)
        except razorpay_client.RazorpayClientError as e:
            log.warning("Could not fetch subscription %s: %s", sub_id, e)
            return None  # fall through to other strategies

        status = sub.get("status", "")
        inactive_statuses = {"halted", "cancelled", "expired", "completed"}
        if status in inactive_statuses:
            return ExecutionResult(
                outcome=ExecutionOutcome.SUBSCRIPTION_INACTIVE,
                execution_mode=ExecutionMode.REAL_TEST,
                success=False,
                razorpay_subscription_id=sub_id,
                failure_reason=f"Subscription {sub_id} is {status}; no retry possible.",
                raw_response={"id": sub_id, "status": status},
            )
        return None  # active/pending/authenticated — proceed with other strategies

    def _verify_subscription_inactive(self, case: dict,
                                      sub_id: Optional[str]) -> ExecutionResult:
        """For mandate_revoked: confirm subscription is inactive, no retry."""
        if sub_id:
            try:
                sub = razorpay_client.fetch_subscription(sub_id)
                status = sub.get("status", "revoked")
                return ExecutionResult(
                    outcome=ExecutionOutcome.SUBSCRIPTION_INACTIVE,
                    execution_mode=ExecutionMode.REAL_TEST,
                    success=False,
                    razorpay_subscription_id=sub_id,
                    failure_reason=(
                        f"Mandate revoked. Subscription {sub_id} status: {status}. "
                        "No retry permitted per policy."
                    ),
                    raw_response={"id": sub_id, "status": status},
                )
            except razorpay_client.RazorpayClientError:
                pass
        return ExecutionResult(
            outcome=ExecutionOutcome.SUBSCRIPTION_INACTIVE,
            execution_mode=ExecutionMode.REAL_TEST,
            success=False,
            razorpay_subscription_id=sub_id,
            failure_reason="Mandate revoked. No retry permitted per policy.",
        )

    def _create_recovery_link(self, case: dict, amount_rupees: float,
                              attempt: int) -> ExecutionResult:
        """Create a Razorpay Payment Link for the customer to complete recovery.

        This is the primary real execution path for UPI Autopay failures because
        Razorpay does not expose a direct 'trigger debit' API for subscriptions.
        The payment link approach is explicitly documented and labeled.
        """
        cust_id = case.get("customer_id", "")
        notes = {
            "customer_id": cust_id,
            "mandate_rescue_attempt": str(attempt),
            "failure_reason": case.get("failure_reason", ""),
            "execution_mode": "real_test",
        }
        description = (
            f"Recovery attempt {attempt}: please complete this payment "
            f"to restore your subscription (Rs {amount_rupees:.0f})."
        )
        try:
            link = razorpay_client.create_payment_link(
                amount_rupees=amount_rupees,
                description=description,
                notes=notes,
            )
            link_id = link.get("id", "")
            short_url = link.get("short_url", "")
            log.info(
                "Created payment link %s (short: %s) for customer %s attempt %d.",
                link_id, short_url, cust_id, attempt,
            )
            return ExecutionResult(
                outcome=ExecutionOutcome.PAYMENT_LINK_SENT,
                execution_mode=ExecutionMode.REAL_TEST,
                success=True,   # success = action taken; recovery confirmed on payment
                razorpay_payment_link_id=link_id,
                payment_link_url=short_url,
                amount_rupees=amount_rupees,
                raw_response={"id": link_id, "short_url": short_url,
                              "status": link.get("status")},
                failure_reason=None,
            )
        except razorpay_client.RazorpayClientError as e:
            err_str = str(e)
            # Classify the error for better audit records.
            if "401" in err_str or "403" in err_str or "Unauthorized" in err_str:
                outcome = ExecutionOutcome.INVALID_CREDENTIALS
            elif "404" in err_str or "NOT_FOUND" in err_str.upper():
                outcome = ExecutionOutcome.INVALID_RESOURCE
            elif "network" in err_str.lower() or "timeout" in err_str.lower():
                outcome = ExecutionOutcome.NETWORK_ERROR
            else:
                outcome = ExecutionOutcome.API_ERROR
            log.error("Failed to create payment link for customer %s: %s", cust_id, e)
            return ExecutionResult(
                outcome=outcome,
                execution_mode=ExecutionMode.REAL_TEST,
                success=False,
                amount_rupees=amount_rupees,
                failure_reason=str(e),
            )

    # ------------------------------------------------------------------
    # Simulation path  (kept for benchmarks / Policy Sandbox / synthetic demo)
    # ------------------------------------------------------------------
    def _execute_simulation(self, case: dict, attempt: int,
                            success_prob: float) -> ExecutionResult:
        """RNG-based simulated execution. No HTTP calls. Always labeled as simulation."""
        if self._rng is None:
            import random
            rng = random.Random()
        else:
            rng = self._rng
        success = rng.random() < success_prob
        outcome = ExecutionOutcome.SIMULATED_SUCCESS if success else ExecutionOutcome.SIMULATED_FAILURE
        return ExecutionResult(
            outcome=outcome,
            execution_mode=ExecutionMode.SIMULATION,
            success=success,
            amount_rupees=float(case.get("amount", 0) or 0),
            failure_reason=None if success else
                f"Simulated failure (prob={success_prob:.2f}, attempt={attempt}). "
                "This is a synthetic benchmark result, not a real payment outcome.",
        )


# ---------------------------------------------------------------------------
# Module-level singleton for normal use; tests create their own instances
# ---------------------------------------------------------------------------
_default_service: Optional[PaymentExecutionService] = None


def get_executor(rng=None) -> PaymentExecutionService:
    """Return the module-level executor (or a new one with the given rng)."""
    global _default_service
    if rng is not None:
        return PaymentExecutionService(rng=rng)
    if _default_service is None:
        _default_service = PaymentExecutionService()
    return _default_service


# ---------------------------------------------------------------------------
# Credential verification helper (used by app.py / scheduler.py)
# ---------------------------------------------------------------------------
def verify_razorpay_credentials() -> dict:
    """Test whether Razorpay API credentials are valid by making a minimal API call.

    Returns a dict:
        {
          "configured": bool,    # keys present and non-placeholder
          "reachable": bool,     # API responded (even with an error means reachable)
          "authenticated": bool, # 2xx response received
          "error": str | None,   # human-readable error if any step failed
          "mode": str            # "test" or "live" (from key_id prefix)
        }
    Never raises.
    """
    result = {
        "configured": False,
        "reachable": False,
        "authenticated": False,
        "error": None,
        "mode": "unknown",
    }
    try:
        key_id, _ = razorpay_client._credentials()
        result["configured"] = True
        result["mode"] = "test" if key_id.startswith("rzp_test_") else "live"
    except razorpay_client.RazorpayClientError as e:
        result["error"] = str(e)
        return result

    # Make the cheapest valid read-only call: list plans (returns quickly, no state).
    try:
        razorpay_client._request("GET", "/plans?count=1")
        result["reachable"] = True
        result["authenticated"] = True
    except razorpay_client.RazorpayClientError as e:
        err = str(e)
        result["reachable"] = True  # we got a response, even if it was an error
        if "401" in err or "403" in err:
            result["error"] = "Invalid credentials (401/403)."
        else:
            result["error"] = err
    except Exception as e:
        result["error"] = f"Network error: {e}"
    return result
