
"""B2B Receivables Chaser - Phase 7.
Invoice tracking, aging, intelligent prioritization, escalation lifecycle.
"""
import json, logging, uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
import db
import recovery_orchestrator as orch

log = logging.getLogger("mandate_rescue.b2b")
_NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")

STATUS_DUE        = "due"
STATUS_REMINDED   = "reminded"
STATUS_OVERDUE    = "overdue"
STATUS_FOLLOW_UP  = "follow_up"
STATUS_PROMISED   = "promised"
STATUS_ESCALATED  = "escalated"
STATUS_PAID       = "paid"
STATUS_WRITTEN_OFF= "written_off"

PRIORITY_FACTORS = {
    "overdue_days": 0.35,
    "amount":       0.30,
    "history":      0.20,
    "likelihood":   0.15,
}


def create_invoice(conn, merchant_id, customer_name, amount, due_at,
                   customer_email=None, customer_phone=None,
                   customer_company=None, invoice_number=None,
                   notes=None, is_demo=0):
    """Create a B2B invoice and corresponding recovery case."""
    inv_id = str(uuid.uuid4())
    now    = _NOW()
    try:
        due_dt = datetime.fromisoformat(due_at.replace("Z","+00:00"))
    except Exception:
        due_dt = datetime.now(timezone.utc) + timedelta(days=30)
    overdue_days = max(0,(datetime.now(timezone.utc)-due_dt).days)
    status = STATUS_OVERDUE if overdue_days>0 else STATUS_DUE
    priority = _priority(amount, overdue_days)
    conn.execute(
        """INSERT INTO b2b_invoices
           (invoice_id,merchant_id,customer_name,customer_email,customer_phone,
            customer_company,invoice_number,amount,issued_at,due_at,
            overdue_days,status,priority,source,created_at,updated_at,is_demo)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'SIMULATED',?,?,?)""",
        (inv_id,merchant_id,customer_name,customer_email,customer_phone,
         customer_company,invoice_number,amount,now,due_at,
         overdue_days,status,priority,now,now,is_demo))
    # Unified case
    case_id = orch.create_case(
        conn, merchant_id=merchant_id,
        scenario_type=orch.SCENARIO_B2B_RECEIVABLE,
        amount=amount, amount_at_risk=amount,
        customer_name=customer_name, customer_email=customer_email,
        customer_phone=customer_phone,
        priority=priority, source="SIMULATED", is_demo=is_demo,
        due_at=due_at)
    conn.execute("UPDATE b2b_invoices SET recovery_case_id=? WHERE invoice_id=?",
                 (case_id, inv_id))
    conn.commit()
    orch.detect_and_score(conn, case_id, merchant_id)
    orch.decide_action(conn, case_id, merchant_id)
    conn.commit()
    return inv_id, case_id


def _priority(amount, overdue_days):
    score = 0
    if amount>100000: score+=3
    elif amount>20000: score+=2
    elif amount>5000:  score+=1
    if overdue_days>60: score+=3
    elif overdue_days>30: score+=2
    elif overdue_days>7:  score+=1
    if score>=5: return "critical"
    if score>=3: return "high"
    if score>=2: return "medium"
    return "low"


def send_reminder(conn, merchant_id, invoice_id, execution_mode="SIMULATED"):
    """Send a payment reminder for an invoice."""
    inv = _get_invoice(conn, invoice_id, merchant_id)
    if not inv: return {"ok":False,"error":"not_found"}
    now = _NOW()
    cnt = (inv.get("reminder_count") or 0)+1
    conn.execute(
        "UPDATE b2b_invoices SET status=?,last_reminder_at=?,reminder_count=?,updated_at=? "
        "WHERE invoice_id=?",
        (STATUS_REMINDED,now,cnt,now,invoice_id))
    conn.commit()
    if inv.get("recovery_case_id"):
        orch.execute_action(conn, inv["recovery_case_id"], merchant_id,
                            action_type="send_invoice_reminder",
                            execution_mode=execution_mode)
    return {"ok":True,"invoice_id":invoice_id,"reminder_count":cnt,
            "execution_mode":execution_mode}


def escalate_invoice(conn, merchant_id, invoice_id, notes=None):
    """Escalate an overdue invoice."""
    inv = _get_invoice(conn, invoice_id, merchant_id)
    if not inv: return {"ok":False,"error":"not_found"}
    conn.execute(
        "UPDATE b2b_invoices SET status=?,updated_at=?,notes=? WHERE invoice_id=?",
        (STATUS_ESCALATED,_NOW(),notes,invoice_id))
    conn.commit()
    if inv.get("recovery_case_id"):
        orch.record_outcome(conn,inv["recovery_case_id"],merchant_id,"escalated")
    return {"ok":True,"invoice_id":invoice_id,"status":"escalated"}


def mark_paid(conn, merchant_id, invoice_id, paid_amount=None):
    inv = _get_invoice(conn, invoice_id, merchant_id)
    if not inv: return {"ok":False,"error":"not_found"}
    rv = paid_amount or float(inv.get("amount") or 0)
    conn.execute(
        "UPDATE b2b_invoices SET status=?,paid_at=?,paid_amount=?,updated_at=? WHERE invoice_id=?",
        (STATUS_PAID,_NOW(),rv,_NOW(),invoice_id))
    conn.commit()
    if inv.get("recovery_case_id"):
        orch.record_outcome(conn,inv["recovery_case_id"],merchant_id,"recovered",rv)
    return {"ok":True,"invoice_id":invoice_id,"paid_amount":rv}


def _get_invoice(conn, invoice_id, merchant_id):
    r = conn.execute("SELECT * FROM b2b_invoices WHERE invoice_id=? AND merchant_id=?",
                     (invoice_id,merchant_id)).fetchone()
    return dict(r) if r else None


def get_invoices(conn, merchant_id, status=None, is_demo=None, limit=100):
    cl,pa = ["merchant_id=?"],[merchant_id]
    if status:          cl.append("status=?");    pa.append(status)
    if is_demo is not None: cl.append("is_demo=?"); pa.append(is_demo)
    rows = conn.execute(
        f"SELECT * FROM b2b_invoices WHERE {' AND '.join(cl)} "
        "ORDER BY overdue_days DESC, amount DESC LIMIT ?", pa+[limit]).fetchall()
    return [dict(r) for r in rows]


def aging_summary(conn, merchant_id, is_demo=None):
    """Aging bucket summary."""
    invoices = get_invoices(conn, merchant_id, is_demo=is_demo, limit=5000)
    buckets = {"0_30":{"count":0,"amount":0.0},"31_60":{"count":0,"amount":0.0},
               "61_90":{"count":0,"amount":0.0},"90_plus":{"count":0,"amount":0.0}}
    total_outstanding = 0.0
    for inv in invoices:
        if inv["status"] in (STATUS_PAID, STATUS_WRITTEN_OFF): continue
        d = inv.get("overdue_days",0) or 0
        a = float(inv.get("amount") or 0)
        total_outstanding += a
        if d<=30:       buckets["0_30"]["count"]+=1;    buckets["0_30"]["amount"]+=a
        elif d<=60:     buckets["31_60"]["count"]+=1;   buckets["31_60"]["amount"]+=a
        elif d<=90:     buckets["61_90"]["count"]+=1;   buckets["61_90"]["amount"]+=a
        else:           buckets["90_plus"]["count"]+=1; buckets["90_plus"]["amount"]+=a
    for b in buckets.values(): b["amount"] = round(b["amount"],2)
    return {"data_type":"ACTUAL","total_outstanding":round(total_outstanding,2),
            "buckets":buckets}


def seed_demo_invoices(conn, merchant_id):
    """Seed demo B2B invoices."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    invoices = [
        ("Acme Corp","acme@example.com",85000,(now-timedelta(days=45)).isoformat(),"INV-001"),
        ("TechBuild Ltd","tech@example.com",32000,(now-timedelta(days=12)).isoformat(),"INV-002"),
        ("GlobalTrade","trade@example.com",150000,(now-timedelta(days=72)).isoformat(),"INV-003"),
        ("SmallBiz Co","small@example.com",8500,(now+timedelta(days=5)).isoformat(),"INV-004"),
        ("Infra Partners","infra@example.com",45000,(now-timedelta(days=28)).isoformat(),"INV-005"),
    ]
    result = []
    for cname,email,amt,due,num in invoices:
        inv_id,case_id = create_invoice(
            conn,merchant_id=merchant_id,customer_name=cname,
            amount=amt,due_at=due,customer_email=email,
            invoice_number=num,is_demo=1)
        result.append({"invoice_id":inv_id,"case_id":case_id,"amount":amt})
    return result
