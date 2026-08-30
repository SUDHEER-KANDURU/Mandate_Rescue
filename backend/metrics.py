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
        total = len(cases)
        amount_at_risk = sum(float(c["amount"]) for c in cases)
        recovered_cases = [c for c in cases if c["case_status"] == "recovered"]
        escalated_cases = [c for c in cases if c["case_status"] == "escalated"]
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

    Each entry is a real `webhook_rejected` audit row joined to its case, so the UI
    can visibly prove the security check works.
    """
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        result = []
        for row in db.get_all_audit(conn):
            if row["event_type"] != "webhook_rejected":
                continue
            case = db.get_case(conn, row["customer_id"])
            result.append({
                "customer_id": row["customer_id"],
                "raw_event_type": case.get("raw_event_type") if case else None,
                "amount": float(case["amount"]) if case else None,
                "failure_reason": case.get("failure_reason") if case else None,
                "event_timestamp": row["event_timestamp"],
                "reason": row["reasoning_text"],
            })
        return result
    finally:
        if own:
            conn.close()


def exceptions(conn=None):
    """Return the first-class exceptions list: every case that ended unrecovered.

    Each entry carries the last audit action + reasoning so the panel is honest.
    """
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        result = []
        for case in db.get_all_cases(conn):
            if case["case_status"] not in EXCEPTION_STATUSES:
                continue
            trail = db.get_audit_for_case(conn, case["customer_id"])
            last = trail[-1] if trail else None
            result.append({
                "customer_id": case["customer_id"],
                "amount": float(case["amount"]),
                "failure_reason": case["failure_reason"],
                "merchant_category": case["merchant_category"],
                "case_status": case["case_status"],
                "last_action": last["action_taken"] if last else "",
                "why_unrecovered": last["reasoning_text"] if last else "",
            })
        result.sort(key=lambda r: r["amount"], reverse=True)
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
