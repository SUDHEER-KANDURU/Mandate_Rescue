"""Synthetic seed generator (design.md section 3.3).

Generates 180 mandate_failures records with the required distributions, using a fixed
RNG seed for reproducibility (N4). Also populates the R13/R14 storage fields
(raw_event_type, mandate_limit) and per-customer salary-window history hints so later
phases have realistic data; no R13-R16 *behavior* is implemented here.
"""

import random
from datetime import datetime, timedelta

import db

SEED = 42
TOTAL = 180

# Failure-reason distribution (must sum to TOTAL): 45 / 20 / 20 / 15 percent.
REASON_COUNTS = {
    "insufficient_funds": 81,     # 45%
    "mandate_expired": 36,        # 20%
    "bank_technical_error": 36,   # 20%
    "mandate_revoked": 27,        # 15%
}

MERCHANT_CATEGORIES = ["subscription", "emi", "insurance", "utility"]

# Razorpay-style webhook event names mapped from failure_reason (R13 storage).
RAW_EVENT_BY_REASON = {
    "insufficient_funds": "subscription.charged.failed",
    "mandate_expired": "payment.failed",
    "bank_technical_error": "payment.failed",
    "mandate_revoked": "subscription.halted",
}


def _weighted_amount(rng):
    """Amount in Rs 199-15000, weighted toward the Rs 500-3000 band."""
    # 70% of records fall in the common band; the rest spread across the full range.
    if rng.random() < 0.70:
        return round(rng.uniform(500, 3000), 2)
    return round(rng.uniform(199, 15000), 2)


def _history_days(rng):
    """Comma-separated day-of-month hints for prior successful payments.

    ~60% of customers get >=3 points (enough for v2 inference); the rest get 0-2.
    """
    if rng.random() < 0.60:
        n = rng.randint(3, 6)
        base = rng.choice([2, 3, 7, 15, 28, 30])
        days = [min(31, max(1, base + rng.randint(-1, 1))) for _ in range(n)]
        return ",".join(str(d) for d in days)
    n = rng.randint(0, 2)
    return ",".join(str(rng.randint(1, 31)) for _ in range(n))


def _mandate_limit(rng, amount):
    """Default UPI mandate limit Rs 5000; seed ~12% of cases above their limit (R14)."""
    if rng.random() < 0.12:
        # Force an over-limit edge case: limit below amount (but at least 1000).
        return 5000 if amount > 5000 else float(rng.choice([1000, 2000, 3000]))
    # Otherwise a comfortable limit at or above the amount.
    return 5000 if amount <= 5000 else round(amount + rng.uniform(1000, 5000), 2)


def build_records(rng):
    """Return a list of 180 mandate_failures record dicts."""
    reasons = []
    for reason, count in REASON_COUNTS.items():
        reasons.extend([reason] * count)
    rng.shuffle(reasons)

    today = datetime.now()
    records = []
    for i, reason in enumerate(reasons):
        amount = _weighted_amount(rng)
        failure_date = (today - timedelta(days=rng.randint(0, 29))).date().isoformat()
        record = {
            "customer_id": f"CUST{1000 + i}",
            "amount": amount,
            "failure_reason": reason,
            "failure_date": failure_date,
            "past_retry_count": rng.randint(0, 2),
            "customer_tenure_months": rng.randint(1, 48),
            "past_payment_success_rate": round(rng.uniform(0.3, 0.99), 2),
            "merchant_category": rng.choice(MERCHANT_CATEGORIES),
            "case_status": "new",
            "raw_event_type": RAW_EVENT_BY_REASON[reason],
            "mandate_limit": _mandate_limit(rng, amount),
            "dunning_stage": 0,
            "history_success_days": _history_days(rng),
        }
        records.append(record)
    return records


def seed_database():
    """Reset the DB and insert the 180 synthetic records. Returns count inserted."""
    rng = random.Random(SEED)
    records = build_records(rng)
    conn = db.get_connection()
    try:
        db.reset_db(conn)
        for record in records:
            db.insert_mandate_failure(conn, record)
        conn.commit()
    finally:
        conn.close()
    return len(records)


if __name__ == "__main__":
    n = seed_database()
    print(f"Seeded {n} mandate_failures records into {db.DB_PATH}")
