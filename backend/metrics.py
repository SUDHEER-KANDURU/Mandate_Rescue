"""Dashboard metrics (design.md section 8).

Every figure is computed from mandate_failures final status and audit_log rows. No
number is hardcoded (N1). Exceptions are treated as a first-class output (N2).
"""

import db


def _final_statuses(conn):
    return {row["customer_id"]: row for row in db.get_all_cases(conn)}


def core_metrics(conn=None):
    """Return the headline KPI dict, all derived from real rows."""
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        cases = db.get_all_cases(conn)
        # Exclude 'invalid' events (failed input validation, e.g. a negative amount)
        # and 'duplicate' replay-idempotency markers from money and rate aggregates:
        # neither ever entered the scoring pipeline, so including them would corrupt
        # totals. 'rejected' (bad signature) is kept in the denominator as before.
        NON_PIPELINE_STATUSES = ("invalid", "duplicate")
        counted = [c for c in cases if c["case_status"] not in NON_PIPELINE_STATUSES]
        total = len(counted)
        amount_at_risk = sum(float(c["amount"]) for c in counted)
        recovered_cases = [c for c in counted if c["case_status"] == "recovered"]
        escalated_cases = [c for c in counted if c["case_status"] == "escalated"]
        amount_recovered = sum(float(c["amount"]) for c in recovered_cases)

        recovery_rate = (len(recovered_cases) / total) if total else 0.0
        escalation_rate = (len(escalated_cases) / total) if total else 0.0
        amount_recovery_rate = (amount_recovered / amount_at_risk) if amount_at_risk else 0.0

        return {
            "total_cases": total,
            "amount_at_risk": round(amount_at_risk, 2),
            "amount_recovered": round(amount_recovered, 2),
            "recovered_cases": len(recovered_cases),
            "escalated_cases": len(escalated_cases),
            "recovery_rate": round(recovery_rate, 4),
            "escalation_rate": round(escalation_rate, 4),
            "amount_recovery_rate": round(amount_recovery_rate, 4),
        }
    finally:
        if own:
            conn.close()


# Statuses that represent an unrecovered, honestly-surfaced outcome (N2).
EXCEPTION_STATUSES = ("escalated", "broken_promise")


def rejected_webhooks(conn=None):
    """Return events blocked at ingestion for failing signature verification.

    Performance: single JOIN query replaces the previous N+1 pattern
    (get_all_audit + per-row get_case).
    """
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT a.customer_id, a.event_timestamp, a.reasoning_text,
                   m.raw_event_type, m.amount, m.failure_reason
            FROM audit_log a
            LEFT JOIN mandate_failures m ON a.customer_id = m.customer_id
            WHERE a.event_type = 'webhook_rejected'
            ORDER BY a.event_id
            """
        ).fetchall()
        return [
            {
                "customer_id": r["customer_id"],
                "raw_event_type": r["raw_event_type"],
                "amount": float(r["amount"]) if r["amount"] is not None else None,
                "failure_reason": r["failure_reason"],
                "event_timestamp": r["event_timestamp"],
                "reason": r["reasoning_text"],
            }
            for r in rows
        ]
    finally:
        if own:
            conn.close()


def exceptions(conn=None):
    """Return the first-class exceptions list: every case that ended unrecovered.

    Performance: one JOIN query to get the last audit action per case replaces
    the previous N+1 pattern (get_all_cases + per-case get_audit_for_case).
    """
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        # Fetch all exception cases in one query.
        cases = conn.execute(
            """
            SELECT customer_id, amount, failure_reason, merchant_category, case_status
            FROM mandate_failures
            WHERE case_status IN ('escalated', 'broken_promise')
            ORDER BY amount DESC
            """
        ).fetchall()
        if not cases:
            return []
        # Fetch the last audit event for each of those customer_ids in one query.
        cids = tuple(r["customer_id"] for r in cases)
        placeholders = ",".join("?" * len(cids))
        last_events = conn.execute(
            f"""
            SELECT a.customer_id, a.action_taken, a.reasoning_text
            FROM audit_log a
            INNER JOIN (
                SELECT customer_id, MAX(event_id) AS max_eid
                FROM audit_log
                WHERE customer_id IN ({placeholders})
                GROUP BY customer_id
            ) last ON a.customer_id = last.customer_id AND a.event_id = last.max_eid
            """,
            cids,
        ).fetchall()
        last_by_cid = {r["customer_id"]: r for r in last_events}
        result = []
        for c in cases:
            last = last_by_cid.get(c["customer_id"])
            result.append({
                "customer_id": c["customer_id"],
                "amount": float(c["amount"]),
                "failure_reason": c["failure_reason"],
                "merchant_category": c["merchant_category"],
                "case_status": c["case_status"],
                "last_action": last["action_taken"] if last else "",
                "why_unrecovered": last["reasoning_text"] if last else "",
            })
        return result
    finally:
        if own:
            conn.close()


def _tenure_bucket(months):
    m = int(months)
    if m <= 6:
        return "0-6 months"
    if m <= 12:
        return "7-12 months"
    if m <= 24:
        return "13-24 months"
    return "25+ months"


TENURE_BUCKET_ORDER = ["0-6 months", "7-12 months", "13-24 months", "25+ months"]


def _rate_breakdown(cases, key_fn):
    buckets = {}
    for c in cases:
        key = key_fn(c)
        b = buckets.setdefault(key, {"total": 0, "recovered": 0, "amount_at_risk": 0.0, "amount_recovered": 0.0})
        b["total"] += 1
        b["amount_at_risk"] += float(c["amount"])
        if c["case_status"] == "recovered":
            b["recovered"] += 1
            b["amount_recovered"] += float(c["amount"])
    out = []
    for key, b in buckets.items():
        b["segment"] = key
        b["recovery_rate"] = round(b["recovered"] / b["total"], 4) if b["total"] else 0.0
        b["amount_at_risk"] = round(b["amount_at_risk"], 2)
        b["amount_recovered"] = round(b["amount_recovered"], 2)
        out.append(b)
    return out


def cohorts(conn=None):
    """Recovery-rate breakdown by tenure bucket and by merchant_category (R10)."""
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        cases = db.get_all_cases(conn)
        by_tenure = _rate_breakdown(cases, lambda c: _tenure_bucket(c["customer_tenure_months"]))
        by_tenure.sort(key=lambda r: TENURE_BUCKET_ORDER.index(r["segment"]) if r["segment"] in TENURE_BUCKET_ORDER else 99)
        by_category = _rate_breakdown(cases, lambda c: c["merchant_category"])
        by_category.sort(key=lambda r: r["segment"])
        return {"by_tenure": by_tenure, "by_category": by_category}
    finally:
        if own:
            conn.close()
