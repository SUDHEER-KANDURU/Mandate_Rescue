"""Webhook signature verification (security hardening, additive).

Razorpay (and most payment providers) sign each webhook with an HMAC of the raw
payload so the receiver can prove the event really came from them and was not
spoofed or tampered with. We mirror that here:

- `sign_payload(payload)` computes an HMAC-SHA256 hex digest over a canonical string
  built from the event's stable fields, keyed with WEBHOOK_SECRET.
- `verify_signature(payload, signature)` recomputes the expected signature and
  compares it in constant time with hmac.compare_digest (avoids timing side
  channels). Returns True only on an exact match.

The secret is read from the environment (WEBHOOK_SECRET) with a placeholder default
so the app still runs if the user has not set one. In a real deployment the default
must be replaced -- see .env.example.
"""

import hashlib
import hmac
import os

# Placeholder default so the demo runs out of the box. Override via env / .env.
DEFAULT_SECRET = "demo-secret-change-me"


def _secret_bytes():
    return (os.environ.get("WEBHOOK_SECRET") or DEFAULT_SECRET).encode("utf-8")


def canonical_string(payload):
    """Build the canonical string that gets signed, from stable event fields.

    Order and formatting are fixed so signing and verification always agree. Amount
    is normalized to 2 decimals so float formatting never causes a mismatch.
    """
    amount = float(payload.get("amount", 0) or 0)
    parts = [
        str(payload.get("customer_id", "")),
        str(payload.get("raw_event_type", "")),
        str(payload.get("failure_date", "")),
        f"{amount:.2f}",
    ]
    return "|".join(parts)


def sign_payload(payload):
    """Return the HMAC-SHA256 hex signature for a webhook payload dict."""
    msg = canonical_string(payload).encode("utf-8")
    return hmac.new(_secret_bytes(), msg, hashlib.sha256).hexdigest()


def verify_signature(payload, signature):
    """Constant-time check that `signature` matches the payload's expected HMAC.

    Returns False (never raises) for a missing/short/mismatched signature so callers
    can treat any failure uniformly as "reject this event".
    """
    if not signature or not isinstance(signature, str):
        return False
    expected = sign_payload(payload)
    return hmac.compare_digest(expected, signature)
