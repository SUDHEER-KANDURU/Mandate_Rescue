"""Thin Razorpay REST API client for creating real TEST-MODE subscriptions.

Zero extra pip dependencies (uses urllib, same pattern as llm_client.py). Reads
credentials from RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET (Dashboard > Settings > API
Keys, Test Mode). This is used by scripts/razorpay_demo_setup.py to create a handful
of real Razorpay test subscriptions/plans so the recovery pipeline has genuine
Razorpay-sourced data to demo against, alongside the 180-case synthetic simulation.

This module ONLY talks to Razorpay to create/fetch subscriptions and plans for demo
setup purposes. It never reads webhook events (that's razorpay_adapter.py) and never
makes any recovery/scoring/compliance decision.

Razorpay Subscriptions API reference: a Plan defines the amount/interval/period; a
Subscription links a plan to a customer and (once authorized) bills on schedule.
https://razorpay.com/docs/api/payments/subscriptions/
"""

import base64
import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("mandate_rescue.razorpay_client")

API_BASE = os.environ.get("RAZORPAY_API_BASE", "https://api.razorpay.com/v1")
REQUEST_TIMEOUT = float(os.environ.get("RAZORPAY_TIMEOUT", "10"))


class RazorpayClientError(RuntimeError):
    """Raised on a Razorpay API call failure (auth, bad request, network, etc.)."""


def _credentials():
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret or key_id.strip() == "" or "your_key" in key_id.lower():
        raise RazorpayClientError(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not configured. Get test-mode "
            "keys from the Razorpay Dashboard (Settings > API Keys) and set them in "
            ".env before creating real subscriptions."
        )
    return key_id, key_secret


def _auth_header():
    key_id, key_secret = _credentials()
    token = base64.b64encode(f"{key_id}:{key_secret}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _request(method, path, body=None):
    url = API_BASE.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": _auth_header(),
            "Content-Type": "application/json",
            "User-Agent": "MandateRescue/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        log.warning("Razorpay API HTTP %s on %s %s: %s", e.code, method, path, detail)
        raise RazorpayClientError(f"Razorpay API error {e.code} on {method} {path}: {detail}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning("Razorpay API network error on %s %s: %s", method, path, e)
        raise RazorpayClientError(f"Razorpay API network error on {method} {path}: {e}") from e


def create_plan(amount_rupees, interval=1, period="monthly", name="Mandate Rescue Demo Plan",
                description="Recurring UPI Autopay demo plan for Mandate Rescue"):
    """Create a real Razorpay TEST-MODE plan. Returns the plan entity dict."""
    body = {
        "period": period,
        "interval": interval,
        "item": {
            "name": name,
            "amount": int(round(amount_rupees * 100)),  # Razorpay amounts are in paise
            "currency": "INR",
            "description": description,
        },
    }
    return _request("POST", "/plans", body)


def create_subscription(plan_id, customer_id, total_count=12, notes=None):
    """Create a real Razorpay TEST-MODE subscription for a plan.

    `notes` is merchant-supplied metadata Razorpay echoes back verbatim on every
    webhook for this subscription — this is how razorpay_adapter.map_razorpay_event
    recovers OUR customer_id and demo context (merchant_category, mandate_limit,
    etc.) from a real Razorpay event without needing a separate lookup table.
    """
    body = {
        "plan_id": plan_id,
        "total_count": total_count,
        "quantity": 1,
        "notes": dict(notes or {}, customer_id=customer_id),
    }
    return _request("POST", "/subscriptions", body)


def fetch_subscription(subscription_id):
    return _request("GET", f"/subscriptions/{subscription_id}")


def cancel_subscription(subscription_id, cancel_at_cycle_end=False):
    return _request("POST", f"/subscriptions/{subscription_id}/cancel",
                    {"cancel_at_cycle_end": 1 if cancel_at_cycle_end else 0})


# ---------------------------------------------------------------------------
# Phase 4: Recovery execution operations
# ---------------------------------------------------------------------------

def fetch_payment(payment_id):
    """Fetch a single payment entity by Razorpay payment_id (pay_xxx).

    Used by the executor to verify a payment's current status and amount
    before deciding whether recovery already succeeded externally.
    """
    return _request("GET", f"/payments/{payment_id}")


def capture_payment(payment_id, amount_rupees, currency="INR"):
    """Capture an authorized payment.

    Razorpay payments start in 'authorized' state; capture moves them to
    'captured' (funds settled). Idempotent: capturing an already-captured
    payment returns a 400 with 'BAD_REQUEST_ERROR' / 'payment_already_captured'.
    Amount must equal the original authorized amount.

    Args:
        payment_id: Razorpay payment id (pay_xxx).
        amount_rupees: amount to capture in rupees (converted to paise internally).
        currency: must match the original payment currency; defaults to "INR".
    Returns:
        The Razorpay payment entity dict.
    Raises:
        RazorpayClientError on network, auth, or API errors.
    """
    body = {
        "amount": int(round(amount_rupees * 100)),
        "currency": currency,
    }
    return _request("POST", f"/payments/{payment_id}/capture", body)


def list_payments_for_subscription(subscription_id, count=10, skip=0):
    """List recent payments associated with a subscription.

    Returns a dict with 'count' and 'items' (list of payment entities).
    Used by the executor to find the most recent payment attempt for a
    subscription so we know its current status before acting.

    Note: Razorpay's pagination uses `count` (max 100) and `skip` (offset).
    """
    path = f"/payments?subscription_id={subscription_id}&count={count}&skip={skip}"
    return _request("GET", path)


def create_payment_link(amount_rupees, customer_email=None, customer_contact=None,
                        description=None, notes=None, expire_by_unix=None):
    """Create a Razorpay Payment Link for mandate recovery nudges.

    Used when the mandate has expired or the limit must be raised — we send the
    customer a payment link they can complete to trigger re-authorization.
    Returns the payment link entity including the short_url for delivery.

    Args:
        amount_rupees: link amount in rupees.
        customer_email: pre-fills the email field on the hosted page.
        customer_contact: pre-fills the phone number (e.g. '+919876543210').
        description: visible to the customer on the hosted page.
        notes: dict of merchant metadata echoed in webhooks.
        expire_by_unix: optional Unix timestamp for link expiry.
    Returns:
        Razorpay payment link entity dict including 'short_url'.
    """
    body = {
        "amount": int(round(amount_rupees * 100)),
        "currency": "INR",
        "description": description or "Mandate recovery — please complete this payment",
    }
    if customer_email or customer_contact:
        body["customer"] = {}
        if customer_email:
            body["customer"]["email"] = customer_email
        if customer_contact:
            body["customer"]["contact"] = customer_contact
    if notes:
        body["notes"] = dict(notes)
    if expire_by_unix:
        body["expire_by"] = int(expire_by_unix)
    return _request("POST", "/payment_links", body)


def fetch_payment_link(payment_link_id):
    """Fetch a payment link by its Razorpay id (plink_xxx)."""
    return _request("GET", f"/payment_links/{payment_link_id}")


def credentials_configured():
    """Return True if RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET look like real keys.

    Used by the executor and app to decide at runtime whether real execution
    is available without raising an exception.
    """
    try:
        _credentials()
        return True
    except RazorpayClientError:
        return False
