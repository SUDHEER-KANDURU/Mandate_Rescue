
"""Promise-to-Pay Tracker - Phase 7.
Captures, tracks and follows up on payment promises.
Lifecycle: PROMISE_MADE -> UPCOMING -> DUE -> PAID | MISSED -> FOLLOW_UP -> ESCALATED
"""
import json, logging, uuid
from datetime import datetime, timezone, timedelta
import db
import recovery_orchestrator as orch

log = logging.getLogger("mandate_rescue.promises")
_NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")

STATUS_UPCOMING  = "upcoming"
STATUS_DUE_TODAY = "due_today"
STATUS_PAID      = "paid"
STATUS_MISSED    = "missed"
STATUS_BROKEN    = "broken"
STATUS_ESCALATED = "escalated"
STATUS_CANCELLED = "cancelled"


def create_promise(conn, merchant_id, promised_amount, promised_date,
                   customer_ref=None, customer_name=None, customer_email=None,
                   case_id=None, invoice_id=None, source="manual",
                   confidence="medium", notes=None, is_demo=0):
    """Record a payment promise."""
    promise_id = str(uuid.uuid4())
    now = _NOW()
    try:
        pd = datetime.fromisoformat(promised_date.replace("Z","+00:00"))
    except Exception:
        pd = datetime.now(timezone.utc)+timedelta(days=7)
    today = datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
    if pd.replace(tzinfo=timezone.utc if pd.tzinfo is None else pd.tzinfo) < today:
        status = STATUS_MISSED
    elif pd.date() == datetime.now(timezone.utc).date():
        status = STATUS_DUE_TODAY
    else:
        status = STATUS_UPCOMING
    follow_up = (pd+timedelta(days=1)).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO promises
           (promise_id,merchant_id,case_id,invoice_id,customer_ref,customer_name,
            customer_email,promised_amount,promised_date,source,confidence,status,
            follow_up_date,notes,data_source,created_at,updated_at,is_demo)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (promise_id,merchant_id,case_id,invoice_id,customer_ref,customer_name,
         customer_email,promised_amount,promised_date,source,confidence,status,
         follow_up,notes,"SIMULATED",now,now,is_demo))
    # Link to recovery case
    if case_id:
        orch._event(conn,case_id,merchant_id,"promise_made",
                    f"Promise recorded: Rs{promised_amount:,.0f} by {promised_date}",
                    metadata={"promise_id":promise_id,"amount":promised_amount,
                              "date":promised_date,"confidence":confidence})
    conn.commit()
    return promise_id


def mark_paid(conn, merchant_id, promise_id, actual_amount=None, paid_at=None):
    p = _get(conn, promise_id, merchant_id)
    if not p: return {"ok":False,"error":"not_found"}
    rv = actual_amount or float(p.get("promised_amount") or 0)
    conn.execute(
        "UPDATE promises SET status=?,actual_paid_amount=?,paid_at=?,updated_at=? WHERE promise_id=?",
        (STATUS_PAID,rv,paid_at or _NOW(),_NOW(),promise_id))
    conn.commit()
    if p.get("case_id"):
        orch.record_outcome(conn,p["case_id"],merchant_id,"recovered",rv)
    return {"ok":True,"promise_id":promise_id,"paid_amount":rv,"status":"paid"}


def mark_missed(conn, merchant_id, promise_id):
    p = _get(conn, promise_id, merchant_id)
    if not p: return {"ok":False,"error":"not_found"}
    conn.execute(
        "UPDATE promises SET status=?,missed_at=?,updated_at=? WHERE promise_id=?",
        (STATUS_MISSED,_NOW(),_NOW(),promise_id))
    conn.commit()
    if p.get("case_id"):
        orch._event(conn,p["case_id"],merchant_id,"promise_missed",
                    f"Promise missed: Rs{p.get('promised_amount'):,.0f} was due {p.get('promised_date')}",
                    data_type="ACTUAL")
    return {"ok":True,"promise_id":promise_id,"status":"missed"}


def escalate(conn, merchant_id, promise_id):
    p = _get(conn, promise_id, merchant_id)
    if not p: return {"ok":False,"error":"not_found"}
    conn.execute(
        "UPDATE promises SET status=?,escalated_at=?,updated_at=? WHERE promise_id=?",
        (STATUS_ESCALATED,_NOW(),_NOW(),promise_id))
    conn.commit()
    if p.get("case_id"):
        orch.record_outcome(conn,p["case_id"],merchant_id,"escalated")
    return {"ok":True,"promise_id":promise_id,"status":"escalated"}


def refresh_statuses(conn, merchant_id):
    """Update promise statuses based on current date."""
    today = datetime.now(timezone.utc).date()
    promises = get_promises(conn, merchant_id, status=STATUS_UPCOMING)
    updated = 0
    for p in promises:
        try:
            pd = datetime.fromisoformat(p["promised_date"].replace("Z","+00:00")).date()
        except Exception:
            continue
        if pd < today:
            conn.execute("UPDATE promises SET status=?,updated_at=? WHERE promise_id=?",
                         (STATUS_MISSED,_NOW(),p["promise_id"]))
            updated+=1
        elif pd == today:
            conn.execute("UPDATE promises SET status=?,updated_at=? WHERE promise_id=?",
                         (STATUS_DUE_TODAY,_NOW(),p["promise_id"]))
            updated+=1
    conn.commit()
    return {"updated":updated}


def _get(conn, promise_id, merchant_id):
    r = conn.execute("SELECT * FROM promises WHERE promise_id=? AND merchant_id=?",
                     (promise_id,merchant_id)).fetchone()
    return dict(r) if r else None


def get_promises(conn, merchant_id, status=None, is_demo=None, limit=100):
    cl,pa = ["merchant_id=?"],[merchant_id]
    if status:          cl.append("status=?");    pa.append(status)
    if is_demo is not None: cl.append("is_demo=?"); pa.append(is_demo)
    rows = conn.execute(
        f"SELECT * FROM promises WHERE {' AND '.join(cl)} "
        "ORDER BY promised_date ASC LIMIT ?", pa+[limit]).fetchall()
    return [dict(r) for r in rows]


def summary(conn, merchant_id, is_demo=None):
    """Promise-to-pay summary metrics."""
    p = [merchant_id]+([is_demo] if is_demo is not None else [])
    suf = " AND is_demo=?" if is_demo is not None else ""
    def q(extra=""): 
        return conn.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(promised_amount),0) v "
            f"FROM promises WHERE merchant_id=?{suf}{extra}", p).fetchone()
    total   = q()
    paid    = q(" AND status='paid'")
    missed  = q(" AND status='missed'")
    due_t   = q(" AND status='due_today'")
    upcoming= q(" AND status='upcoming'")
    conv    = round(paid["n"]/total["n"]*100,1) if total["n"]>0 else 0.0
    return {
        "data_type":"ACTUAL",
        "total_promises":total["n"], "total_promised_rs":round(float(total["v"] or 0),2),
        "paid_promises":paid["n"],   "paid_rs":round(float(paid["v"] or 0),2),
        "missed_promises":missed["n"],"missed_rs":round(float(missed["v"] or 0),2),
        "due_today":due_t["n"],      "upcoming":upcoming["n"],
        "conversion_rate_pct":conv,
    }


def seed_demo_promises(conn, merchant_id):
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    samples = [
        (15000, (now+timedelta(days=2)).isoformat(),"C001","Ravi Sharma","ravi@example.com","high"),
        (45000, (now+timedelta(days=1)).isoformat(),"C002","Priya Ltd","priya@example.com","medium"),
        (8000,  (now-timedelta(days=3)).isoformat(),"C003","Small Co",None,"low"),
        (92000, (now+timedelta(days=5)).isoformat(),"C004","BigCorp","big@example.com","high"),
        (3200,  now.isoformat(),"C005","Quick Pay",None,"medium"),
    ]
    result = []
    for amt,date,ref,name,email,conf in samples:
        pid = create_promise(conn,merchant_id=merchant_id,promised_amount=amt,
                             promised_date=date,customer_ref=ref,customer_name=name,
                             customer_email=email,confidence=conf,is_demo=1)
        result.append({"promise_id":pid,"amount":amt,"date":date})
    return result
