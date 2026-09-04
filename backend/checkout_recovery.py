
"""Checkout Abandonment Recovery - Phase 7.
Detects abandoned checkouts, creates recovery cases, manages recovery lifecycle.
All data is SIMULATED unless explicitly marked REAL from a Razorpay webhook.
"""
import json, logging, uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
import db
import recovery_orchestrator as orch

log = logging.getLogger("mandate_rescue.checkout")
_NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")

STAGES = ["initiated","address_entered","payment_method_selected","payment_attempted","abandoned"]
RECOVERY_LINK_TTL_HOURS = 48


def register_abandonment(conn, merchant_id, amount, stage_reached="abandoned",
                         customer_email=None, customer_phone=None, customer_ref=None,
                         payment_method=None, metadata=None, is_demo=0):
    """Register a checkout abandonment and create a recovery case.
    Returns (session_id, case_id).  source=SIMULATED unless called from a REAL webhook.
    """
    session_id = str(uuid.uuid4())
    now = _NOW()
    recovery_link = f"/checkout/recover/{session_id}"
    link_exp = (datetime.now(timezone.utc)+timedelta(hours=RECOVERY_LINK_TTL_HOURS)
                ).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO checkout_sessions
           (session_id,merchant_id,customer_ref,customer_email,customer_phone,
            amount,payment_method,stage_reached,abandoned_at,status,
            recovery_link,recovery_link_expires_at,source,created_at,is_demo,metadata)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'SIMULATED',?,?,?)""",
        (session_id,merchant_id,customer_ref,customer_email,customer_phone,
         amount,payment_method,stage_reached,now,"abandoned",
         recovery_link,link_exp,now,is_demo,
         json.dumps(metadata) if metadata else None))
    # Create unified recovery case
    case_id = orch.create_case(
        conn, merchant_id=merchant_id,
        scenario_type=orch.SCENARIO_CHECKOUT_ABANDONMENT,
        amount=amount, amount_at_risk=amount,
        customer_email=customer_email, customer_phone=customer_phone,
        customer_ref=customer_ref, payment_method=payment_method,
        source="SIMULATED", is_demo=is_demo,
        priority=_priority(amount))
    conn.execute("UPDATE checkout_sessions SET recovery_case_id=? WHERE session_id=?",
                 (case_id,session_id))
    conn.commit()
    orch.detect_and_score(conn, case_id, merchant_id)
    orch.decide_action(conn, case_id, merchant_id)
    conn.commit()
    return session_id, case_id


def _priority(amount):
    if amount>50000: return "high"
    if amount>10000: return "medium"
    return "low"


def mark_recovered(conn, merchant_id, session_id, realized_value=None):
    """Mark a checkout session as recovered (payment completed)."""
    row = conn.execute(
        "SELECT * FROM checkout_sessions WHERE session_id=? AND merchant_id=?",
        (session_id, merchant_id)).fetchone()
    if not row: return {"ok":False,"error":"not_found"}
    row = dict(row)
    rv = realized_value or float(row.get("amount") or 0)
    conn.execute("UPDATE checkout_sessions SET status='recovered',recovered_at=? WHERE session_id=?",
                 (_NOW(), session_id))
    if row.get("recovery_case_id"):
        orch.record_outcome(conn, row["recovery_case_id"], merchant_id,
                            "recovered", realized_value=rv)
    conn.commit()
    return {"ok":True,"session_id":session_id,"recovered_value":rv}


def get_abandoned_sessions(conn, merchant_id, is_demo=None, limit=100):
    cl = ["merchant_id=?","status='abandoned'"]
    pa = [merchant_id]
    if is_demo is not None: cl.append("is_demo=?"); pa.append(is_demo)
    rows = conn.execute(
        f"SELECT * FROM checkout_sessions WHERE {' AND '.join(cl)} "
        "ORDER BY created_at DESC LIMIT ?", pa+[limit]).fetchall()
    return [dict(r) for r in rows]


def recovery_funnel(conn, merchant_id, is_demo=None):
    """Checkout recovery funnel metrics."""
    suf = " AND is_demo=?" if is_demo is not None else ""
    p   = [merchant_id]+([is_demo] if is_demo is not None else [])
    total   = conn.execute(f"SELECT COUNT(*) n FROM checkout_sessions WHERE merchant_id=?{suf}",p).fetchone()["n"]
    recov   = conn.execute(f"SELECT COUNT(*) n,COALESCE(SUM(amount),0) v FROM checkout_sessions WHERE merchant_id=? AND status='recovered'{suf}",p).fetchone()
    abandon = conn.execute(f"SELECT COUNT(*) n,COALESCE(SUM(amount),0) v FROM checkout_sessions WHERE merchant_id=? AND status='abandoned'{suf}",p).fetchone()
    rate    = round(recov["n"]/total*100,1) if total>0 else 0.0
    return {
        "data_type":"ACTUAL",
        "total_sessions":total,
        "abandoned_sessions":abandon["n"],
        "recovered_sessions":recov["n"],
        "recovery_rate_pct":rate,
        "abandoned_value_rs":round(float(abandon["v"] or 0),2),
        "recovered_value_rs":round(float(recov["v"] or 0),2),
        "opportunity_rs":round(float(abandon["v"] or 0)*0.22,2),  # ESTIMATED
        "opportunity_note":"Opportunity is ESTIMATED at 22% baseline recovery rate",
    }


def seed_demo_checkouts(conn, merchant_id):
    """Seed demo checkout sessions (clearly SIMULATED)."""
    import random
    random.seed(99)
    scenarios = [
        (4500,"payment_method_selected","demo@example.com"),
        (12000,"payment_attempted","buyer@test.com"),
        (750,"initiated",None),
        (28000,"payment_attempted","corp@test.com"),
        (3200,"address_entered","user@test.com"),
    ]
    results = []
    for amt, stage, email in scenarios:
        sid, cid = register_abandonment(
            conn, merchant_id=merchant_id, amount=amt,
            stage_reached=stage, customer_email=email,
            is_demo=1)
        results.append({"session_id":sid,"case_id":cid,"amount":amt})
    return results
