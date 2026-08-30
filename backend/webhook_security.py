"""Webhook signature verification (security hardening, additive).

Razorpay (and most payment providers) sign each webhook with an HMAC of the raw
payload so the receiver can prove the event really came from them and was not
spoofed or tampered with. We mirror that here for the synthetic demo pipeline:

- `sign_payload(payload)` computes an HMAC-SHA256 hex digest over a canonical string
  built from the event's stable fields, keyed with WEBHOOK_SECRET.
- `verify_signature(payload, signature)` recomputes the expected signature and
  compares it in constant time with hmac.compare_digest (avoids timing side
  channels). Returns True only on an exact match.

Real Razorpay webhooks (see razorpay_adapter.py) use a different, Razorpay-defined
scheme: HMAC-SHA256 hex over the *raw request body*, keyed with a secret configured
in the Razorpay dashboard (RAZORPAY_WEBHOOK_SECRET, kept separate from this module's
WEBHOOK_SECRET). That verification lives in razorpay_adapter.py; this module is only
for the synthetic seed/demo pipeline's own signing scheme.

Fail-closed secret handling
----------------------------
There is NO insecure default secret. If WEBHOOK_SECRET is unset, empty, or equal to
a known placeholder value (e.g. the literal text from .env.example), every signing
and verification call raises/fails rather than silently trusting a publicly-known
string. This matters because a hardcoded fallback secret is exactly the kind of
vulnerability an attacker (or a security-review agent) checks for first: anyone who
has read this source file would know the fallback key and could forge valid
signatures. See `_secret_bytes()` below.
"""

import hashlib
import hmac
import logging
import os

log = logging.getLogger("mandate_rescue.webhook_security")

# Known placeholder/example values that must NEVER be treated as a real secret. Any
# of these being set (a stale copy of .env.example, an unset variable, a lazily typed
# stand-in) means the "secret" is not actually secret. Comparison is case-insensitive
# and whitespace-trimmed.
_INSECURE_PLACEHOLDERS = frozenset({
    "", "your_secret_here", "your-secret-here", "demo-secret-change-me",
    "changeme", "change-me", "change_me", "secret", "webhook_secret", "test",
    "password", "12345", "changethis",
})


class WebhookSecretError(RuntimeError):
    """Raised when WEBHOOK_SECRET is missing or is a known-insecure placeholder."""


def _secret_bytes():
    """Return the configured WEBHOOK_SECRET as bytes, or raise if missing/insecure.

    Fails closed: there is no default value. Callers that need to keep running
    gracefully on a bad configuration (e.g. inbound signature verification) must
    catch WebhookSecretError themselves and treat it as "reject this event" — see
    `verify_signature()`. Callers that are actively signing new data (seeding,
    generating a demo signature) let this raise, because generating a "signed"
    payload under a known-insecure key is worse than failing loudly at startup.
    """
    value = os.environ.get("WEBHOOK_SECRET")
    if not value or value.strip().lower() in _INSECURE_PLACEHOLDERS:
        raise WebhookSecretError(
            "WEBHOOK_SECRET is not configured with a real secret (it is unset, "
            "empty, or a known placeholder value like 'your_secret_here'). Set a "
            "strong random value in your environment or .env file before signing "
            "or verifying any webhook. Generate one with:\n"
            '  python -c "import secrets; print(secrets.token_hex(32))"'
        )
    return value.encode("utf-8")


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
    """Return the HMAC-SHA256 hex signature for a webhook payload dict.

    Raises WebhookSecretError if WEBHOOK_SECRET is missing/insecure (fail closed —
    see module docstring). Callers that sign data (seeding, demo generation) should
    let this propagate; a misconfigured secret must stop the process, not silently
    produce a signature anyone could reproduce.
    """
    msg = canonical_string(payload).encode("utf-8")
    return hmac.new(_secret_bytes(), msg, hashlib.sha256).hexdigest()


def verify_signature(payload, signature):
    """Constant-time check that `signature` matches the payload's expected HMAC.

    Returns False (never raises) for a missing/short/mismatched signature, AND for a
    missing/insecure WEBHOOK_SECRET configuration — a misconfigured secret means we
    trust nothing, so every inbound event is rejected rather than accidentally
    verified against a publicly-known placeholder. The configuration problem is
    logged loudly server-side so it's diagnosable, without turning every webhook
    request into a 500.
    """
    if not signature or not isinstance(signature, str):
        return False
    try:
        expected = sign_payload(payload)
    except WebhookSecretError:
        log.error(
            "Rejecting webhook: WEBHOOK_SECRET is not configured with a real "
            "secret. No event can be verified until this is fixed — see "
            "webhook_security.WebhookSecretError for how to generate one."
        )
        return False
    return hmac.compare_digest(expected, signature)
