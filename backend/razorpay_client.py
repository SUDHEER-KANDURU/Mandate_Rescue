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
