"""Real Razorpay webhook adapter (additive — sits alongside the synthetic pipeline).

Everything else in this codebase (seed.py, webhook_security.py) works over a
SYNTHETIC event shape invented for the demo: a flat dict with our own field names,
signed with our own canonical string + HMAC scheme. That is fine for the 180-case
simulation, but it means the project never actually talks to Razorpay.

This module is the real integration point. It:

1. Verifies a genuine Razorpay webhook signature exactly the way Razorpay specifies:
   HMAC-SHA256 (hex) over the RAW request body bytes, keyed with the webhook secret
   configured in the Razorpay Dashboard (Settings > Webhooks) — NOT the same secret
   as the synthetic WEBHOOK_SECRET, and NOT a JSON-re-serialized copy of the body
   (re-serializing can change byte-for-byte formatting and break the signature).
   Reference: Razorpay signs the raw body and sends the signature in the
   `X-Razorpay-Signature` header (hex-encoded HMAC-SHA256).

2. Maps Razorpay's real webhook event payload shape (nested under
   `payload.<entity>.entity`, e.g. `payload.subscription.entity` or
   `payload.payment.entity`) onto this project's internal case record shape, so a
   real Razorpay event can flow through the exact same DiagnosisAgent /
   agent.WEBHOOK_TO_REASON classification and recovery pipeline as the synthetic
   data — no separate "real" code path for scoring/strategy/compliance.

Trust boundary: this module ONLY authenticates and reshapes the payload. It never
scores, retries, or decides anything — those all still happen in agent.py exactly as
they do for the synthetic pipeline. A real event and a synthetic event look
identical to the rest of the system once they reach RecoveryPipeline.process_case().

Fail-closed secret handling mirrors webhook_security.py: there is no default secret.
"""

from datetime import datetime, timezone
import hashlib
import hmac
import logging
import os

import db

log = logging.getLogger("mandate_rescue.razorpay_adapter")

_ENV_VAR = "RAZORPAY_WEBHOOK_SECRET"

_INSECURE_PLACEHOLDERS = frozenset({
    "", "your_razorpay_dashboard_webhook_secret_here", "changeme", "change-me",
    "secret", "test", "password",
})


class RazorpaySecretError(RuntimeError):
    """Raised when RAZORPAY_WEBHOOK_SECRET is missing or a known placeholder."""


def _secret_bytes():
    value = os.environ.get(_ENV_VAR)
    if not value or value.strip().lower() in _INSECURE_PLACEHOLDERS:
        raise RazorpaySecretError(
            f"{_ENV_VAR} is not configured with a real secret. Set the webhook "
            "secret you configured in the Razorpay Dashboard (Settings > Webhooks) "
            "as an environment variable / .env entry before any real Razorpay "
            "webhook can be verified."
        )
    return value.encode("utf-8")


def verify_razorpay_signature(raw_body, signature_header):
    """Verify a real Razorpay webhook signature.

    Args:
        raw_body: the EXACT raw request body bytes (or str) as received — never a
            re-serialized/re-parsed copy, since that can change formatting and
            silently break the signature check.
        signature_header: the value of the `X-Razorpay-Signature` header.

    Returns True only on an exact, constant-time match. Returns False (never
    raises) for a missing header/body or a misconfigured secret, so a bad
    deployment configuration fails closed (rejects every event) instead of 500ing
    or, worse, silently accepting unverified payloads.
    """
    if not signature_header or not isinstance(signature_header, str):
        return False
    if raw_body is None:
        return False
    if isinstance(raw_body, str):
        raw_body = raw_body.encode("utf-8")
    try:
        secret = _secret_bytes()
    except RazorpaySecretError:
        log.error(
            "Rejecting Razorpay webhook: %s is not configured. No real Razorpay "
            "event can be verified until this is fixed.", _ENV_VAR,
        )
        return False
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def hash_raw_body(raw_body):
    """SHA-256 hex digest of the exact raw webhook bytes (stable duplicate key)."""
    if raw_body is None:
        raw_body = b""
    if isinstance(raw_body, str):
        raw_body = raw_body.encode("utf-8")
    return hashlib.sha256(raw_body).hexdigest()


def extract_razorpay_event_id(payload, raw_body=None):
    """Return a stable id for this delivery.

    Prefer Razorpay's own event `id` / `event_id` when present. If the payload
    has neither (some test-mode bodies omit it), fall back to the SHA-256 of the
    raw body so identical redeliveries still collide on webhook_events.razorpay_event_id.
    """
    if isinstance(payload, dict):
        for key in ("id", "event_id"):
            value = payload.get(key)
            if value:
                return str(value)
    return hash_raw_body(raw_body)


def _log_webhook_duplicate(conn, customer_id):
    """Append a webhook_duplicate audit row if the case row exists (FK-safe)."""
    case = db.get_case(conn, customer_id)
    if case is None:
        return
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.insert_audit(
        conn, customer_id, ts, "webhook_duplicate",
        "Duplicate webhook ignored; event already processed",
        "n/a", 0,
        "Idempotency: this Razorpay event id was already recorded in "
        "webhook_events, so the delivery was acknowledged without inserting "
        "or updating a case and without re-entering the recovery pipeline.",
        case.get("case_status", "new"),
    )


def claim_webhook_event(conn, payload, raw_body, customer_id=None):
    """Idempotency gate used before any case insert/update.

    Returns (is_duplicate, event_id). On a new event, inserts webhook_events
    with processed=0. On a duplicate event id, logs webhook_duplicate (when a
    customer_id is known and the case exists) and returns (True, event_id).
    """
    event_id = extract_razorpay_event_id(payload, raw_body)
    payload_hash = hash_raw_body(raw_body)
    existing = db.get_webhook_event(conn, event_id)
    if existing is not None:
        if customer_id:
            _log_webhook_duplicate(conn, customer_id)
        return True, event_id
    if not db.insert_webhook_event(conn, event_id, payload_hash):
        # Lost the UNIQUE race against a concurrent delivery of the same event.
        if customer_id:
            _log_webhook_duplicate(conn, customer_id)
        return True, event_id
    return False, event_id


# ---------------------------------------------------------------------------
# Payload mapping: Razorpay event -> internal case record
# ---------------------------------------------------------------------------

# Razorpay event names we understand, mapped to this project's internal
# failure_reason taxonomy (same taxonomy agent.WEBHOOK_TO_REASON already uses for
# the synthetic pipeline, kept in sync deliberately).
RAZORPAY_EVENT_TO_REASON = {
    "subscription.charged.failed": "insufficient_funds",
    "payment.failed": "insufficient_funds",
    "subscription.halted": "mandate_revoked",
    "subscription.cancelled": "mandate_revoked",
    "subscription.pending": "mandate_expired",
    "payment.dispute.created": "bank_technical_error",
}

# Default mandate limit assumed when Razorpay's payload doesn't carry an explicit
# UPI mandate limit (e.g. a card/netbanking subscription has no UPI mandate concept
# at all). Mirrors seed.py's default so downstream scoring/gates behave consistently.
_DEFAULT_MANDATE_LIMIT = 5000.0


def _extract_entity(payload, entity_key):
    """Pull payload['payload'][entity_key]['entity'] safely; None if not present."""
    try:
        return payload["payload"][entity_key]["entity"]
    except (KeyError, TypeError):
        return None


def map_razorpay_event(payload):
    """Map a parsed Razorpay webhook JSON body to an internal case record dict.

    Returns None if the event type isn't one we handle (the caller should
    acknowledge receipt with 200 either way — Razorpay expects a 2xx for every
    delivered event, understood or not, or it will keep retrying).

    The returned dict has the SAME shape as seed.py's synthetic records (minus
    `webhook_signature`, which the real pipeline entry point sets separately from
    Razorpay's own signature — see app.py's /api/webhooks/razorpay route), so it can
    be inserted with db.insert_mandate_failure() and run through the identical
    RecoveryPipeline used for synthetic data.
    """
    event = payload.get("event")
    reason = RAZORPAY_EVENT_TO_REASON.get(event)
    if reason is None:
        return None

    # Prefer the subscription entity (has customer/notes context); fall back to the
    # payment entity for pure payment.* events which don't carry a subscription.
    sub = _extract_entity(payload, "subscription")
    pay = _extract_entity(payload, "payment")

    # customer_id: Razorpay's subscription entity carries `notes` (merchant-supplied
    # metadata) — this project asks integrators to pass their own customer_id there
    # when creating the subscription (see razorpay_client.create_subscription). Fall
    # back to Razorpay's own subscription/customer/payment id if no note was set, so
    # the event is never dropped for lacking OUR customer_id convention.
    customer_id = None
    if sub and isinstance(sub.get("notes"), dict):
        customer_id = sub["notes"].get("customer_id")
    if not customer_id and sub:
        customer_id = sub.get("id")
    if not customer_id and pay:
        customer_id = pay.get("id")
    if not customer_id:
        return None

    amount_paise = None
    if pay and pay.get("amount") is not None:
        amount_paise = pay.get("amount")
    elif sub and sub.get("plan_id") and pay is None:
        # A subscription-level event with no payment entity attached (e.g.
        # subscription.halted) has no per-attempt amount; fall back to notes if the
        # integrator supplied one, else 0 signals "no charge amount attached" and the
        # caller should treat it as informational only (handled by the route, which
        # still requires a positive amount to enter the recovery pipeline).
        amount_paise = None

    amount_rupees = (amount_paise / 100.0) if amount_paise is not None else None

    created_at = payload.get("created_at")
    if created_at:
        failure_date = datetime.fromtimestamp(int(created_at), tz=timezone.utc).date().isoformat()
    else:
        failure_date = datetime.now(timezone.utc).date().isoformat()

    notes = sub.get("notes") if sub and isinstance(sub.get("notes"), dict) else {}

    record = {
        "customer_id": str(customer_id),
        "amount": amount_rupees,
        "failure_reason": reason,
        "failure_date": failure_date,
        "past_retry_count": 0,
        "customer_tenure_months": int(notes.get("customer_tenure_months", 1) or 1),
        "past_payment_success_rate": float(notes.get("past_payment_success_rate", 0.8) or 0.8),
        "merchant_category": notes.get("merchant_category", "subscription"),
        "case_status": "new",
        "raw_event_type": event,
        "mandate_limit": float(notes.get("mandate_limit", _DEFAULT_MANDATE_LIMIT) or _DEFAULT_MANDATE_LIMIT),
        "dunning_stage": 0,
        "history_success_days": notes.get("history_success_days", ""),
        # Provenance marker so the dashboard/audit trail can visibly distinguish a
        # real Razorpay-sourced case from a synthetic seeded one.
        "source": "razorpay_live",
    }
    return record
