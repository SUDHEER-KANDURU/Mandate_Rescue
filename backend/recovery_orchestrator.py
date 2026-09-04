
"""Recovery Orchestrator - Phase 7.
DETECT->PREDICT->INVESTIGATE->DECIDE->ACT->OBSERVE->MEASURE->LEARN pipeline.
All 7 scenario types share this orchestration layer.
REAL/SIMULATED/ESTIMATED labels preserved throughout.
"""
import json, logging, uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("mandate_rescue.orchestrator")
_NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")

# Scenario type constants
SCENARIO_PAYMENT_DEGRADATION  = "payment_degradation"
SCENARIO_FAILED_PAYMENT       = "failed_payment"
SCENARIO_FAILED_SUBSCRIPTION  = "failed_subscription"
SCENARIO_MANDATE_RETRY        = "mandate_retry"
SCENARIO_CHECKOUT_ABANDONMENT = "checkout_abandonment"
SCENARIO_B2B_RECEIVABLE       = "b2b_receivable"
SCENARIO_PROMISE_TO_PAY       = "promise_to_pay"
ALL_SCENARIOS = [
    SCENARIO_PAYMENT_DEGRADATION, SCENARIO_FAILED_PAYMENT,
    SCENARIO_FAILED_SUBSCRIPTION, SCENARIO_MANDATE_RETRY,
    SCENARIO_CHECKOUT_ABANDONMENT, SCENARIO_B2B_RECEIVABLE,
    SCENARIO_PROMISE_TO_PAY,
]

# ---- Case CRUD ----

def create_case(conn, merchant_id, scenario_type, **kw):
    case_id = str(uuid.uuid4())
    now = _NOW()
    fields = dict(case_id=case_id, merchant_id=merchant_id,
                  scenario_type=scenario_type, status="open",
                  priority=kw.get("priority","medium"),
                  amount=kw.get("amount",0),
                  amount_at_risk=kw.get("amount_at_risk",kw.get("amount",0)),
                  source=kw.get("source","SIMULATED"),
                  failure_reason=kw.get("failure_reason"),
                  customer_ref=kw.get("customer_ref"),
                  customer_name=kw.get("customer_name"),
                  customer_email=kw.get("customer_email"),
                  customer_phone=kw.get("customer_phone"),
                  payment_method=kw.get("payment_method"),
                  bank_name=kw.get("bank_name"),
                  merchant_category=kw.get("merchant_category"),
                  mandate_customer_id=kw.get("mandate_customer_id"),
                  razorpay_payment_id=kw.get("razorpay_payment_id"),
                  razorpay_subscription_id=kw.get("razorpay_subscription_id"),
                  is_demo=int(kw.get("is_demo",0)),
                  created_at=now, updated_at=now,
                  due_at=kw.get("due_at"))
    cols = [k for k,v in fields.items() if v is not None]
    ph   = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO recovery_cases ({', '.join(cols)}) VALUES ({ph})",
                 [fields[c] for c in cols])
    _event(conn, case_id, merchant_id, "case_created",
           f"Case created: {scenario_type}", data_type=fields["source"])
    return case_id


def get_case(conn, case_id, merchant_id):
    r = conn.execute("SELECT * FROM recovery_cases WHERE case_id=? AND merchant_id=?",
                     (case_id, merchant_id)).fetchone()
    return dict(r) if r else None


def get_cases(conn, merchant_id, status=None, scenario_type=None,
              is_demo=None, limit=200, offset=0):
    cl, pa = ["merchant_id=?"], [merchant_id]
    if status:       cl.append("status=?");        pa.append(status)
    if scenario_type:cl.append("scenario_type=?"); pa.append(scenario_type)
    if is_demo is not None: cl.append("is_demo=?");pa.append(is_demo)
    where = " AND ".join(cl)
    rows = conn.execute(
        f"SELECT * FROM recovery_cases WHERE {where} "
        "ORDER BY created_at DESC LIMIT ? OFFSET ?", pa+[limit,offset]).fetchall()
    return [dict(r) for r in rows]


def update_case(conn, case_id, merchant_id, **fields):
    ALLOWED = {"status","priority","risk_score","root_cause","root_cause_confidence",
               "recommended_action","selected_strategy","expected_recovery_value",
               "realized_value","recovery_probability","preferred_channel",
               "last_channel_used","communication_count","last_contacted_at",
               "approval_required","approval_status","approved_by","approved_at",
               "approval_notes","ai_explanation","ai_explanation_at",
               "resolved_at","policy_version_id"}
    allowed = {k:v for k,v in fields.items() if k in ALLOWED}
    if not allowed: return
    allowed["updated_at"] = _NOW()
    sets = ", ".join(f"{k}=?" for k in allowed)
    conn.execute(f"UPDATE recovery_cases SET {sets} WHERE case_id=? AND merchant_id=?",
                 list(allowed.values())+[case_id, merchant_id])


def _event(conn, case_id, merchant_id, event_type, description,
           actor="system", metadata=None, data_type="SIMULATED"):
    conn.execute(
        "INSERT INTO recovery_case_events "
        "(case_id,merchant_id,event_type,description,actor,metadata,data_type) "
        "VALUES (?,?,?,?,?,?,?)",
        (case_id,merchant_id,event_type,description,actor,
         json.dumps(metadata) if metadata else None, data_type))


def get_timeline(conn, case_id, merchant_id):
    rows = conn.execute(
        "SELECT * FROM recovery_case_events "
        "WHERE case_id=? AND merchant_id=? ORDER BY occurred_at ASC",
        (case_id, merchant_id)).fetchall()
    return [dict(r) for r in rows]


# ---- DETECT + PREDICT ----

def detect_and_score(conn, case_id, merchant_id):
    """Score risk and recovery probability. data_type=ESTIMATED."""
    case = get_case(conn, case_id, merchant_id)
    if not case: return {"ok":False,"error":"case_not_found"}
    risk  = _risk_score(case)
    prob  = _recovery_prob(case)
    ev    = round(prob * float(case.get("amount") or 0), 2)
    prio  = _priority(risk, float(case.get("amount") or 0))
    update_case(conn,case_id,merchant_id,
                risk_score=risk, recovery_probability=prob,
                expected_recovery_value=ev, priority=prio)
    _event(conn,case_id,merchant_id,"risk_scored",
           f"Risk={risk:.0f}/100 P(recovery)={prob:.2f} EV=Rs{ev:.2f} priority={prio}",
           metadata={"risk_score":risk,"recovery_prob":prob,"ev":ev},
           data_type="ESTIMATED")
    return {"ok":True,"risk_score":risk,"recovery_probability":prob,
            "expected_recovery_value":ev,"priority":prio,"data_type":"ESTIMATED"}


def _risk_score(case):
    score = 50.0
    amount = float(case.get("amount") or 0)
    if amount>50000: score+=20
    elif amount>10000: score+=10
    elif amount>1000: score+=5
    s = case.get("scenario_type","")
    if s == SCENARIO_PAYMENT_DEGRADATION: score+=15
    elif s == SCENARIO_B2B_RECEIVABLE:    score+=10
    elif s == SCENARIO_CHECKOUT_ABANDONMENT: score-=10
    reason = case.get("failure_reason","")
    adj = {"mandate_revoked":20,"insufficient_funds":10,"bank_technical_error":-10}
    score += adj.get(reason,0)
    due = case.get("due_at")
    if due:
        try:
            from datetime import timezone as _tz
            due_dt = datetime.fromisoformat(due.replace("Z","+00:00"))
            days_over = (datetime.now(timezone.utc)-due_dt).days
            if days_over>30: score+=20
            elif days_over>7: score+=10
            elif days_over>0: score+=5
        except Exception: pass
    return round(min(max(score,0),100),1)


def _recovery_prob(case):
    base = {SCENARIO_FAILED_PAYMENT:0.62, SCENARIO_FAILED_SUBSCRIPTION:0.58,
            SCENARIO_MANDATE_RETRY:0.55, SCENARIO_CHECKOUT_ABANDONMENT:0.22,
            SCENARIO_B2B_RECEIVABLE:0.45, SCENARIO_PROMISE_TO_PAY:0.67,
            SCENARIO_PAYMENT_DEGRADATION:0.70}.get(case.get("scenario_type",""),0.50)
    adj = {"bank_technical_error":+0.15,"insufficient_funds":+0.05,
           "mandate_expired":-0.10,"mandate_revoked":-0.40}.get(
               case.get("failure_reason",""),0.0)
    amount = float(case.get("amount") or 0)
    if amount>100000: adj-=0.10
    elif amount>50000: adj-=0.05
    return round(min(max(base+adj,0.01),0.99),3)


def _priority(risk, amount):
    if risk>=80 or amount>100000: return "critical"
    if risk>=60 or amount>10000:  return "high"
    if risk>=40:                  return "medium"
    return "low"


# ---- DECIDE ----

def decide_action(conn, case_id, merchant_id):
    """Select action, channel, timing. data_type=ESTIMATED for EV."""
    case = get_case(conn, case_id, merchant_id)
    if not case: return {"ok":False,"error":"case_not_found"}
    policy = _policy(conn, merchant_id)
    amount = float(case.get("amount") or 0)
    ev     = float(case.get("expected_recovery_value") or 0)
    min_ev = float(policy.get("min_expected_value_rs",0))
    if ev>0 and ev<min_ev:
        return {"ok":True,"action":"skip","reason":f"EV Rs{ev:.2f}<threshold Rs{min_ev:.2f}"}
    needs_approval = amount>=float(policy.get("approval_threshold_rs",10000))
    action   = _action_for(case, policy)
    channel  = _channel_for(case, policy)
    language = policy.get("preferred_language","en")
    sched    = _next_slot(policy)
    update_case(conn,case_id,merchant_id,
                recommended_action=action, preferred_channel=channel,
                approval_required=int(needs_approval),
                approval_status="pending" if needs_approval else "not_required",
                status="pending_approval" if needs_approval else "in_progress")
    _event(conn,case_id,merchant_id,"strategy_selected",
           f"Action={action} channel={channel} approval_needed={needs_approval}",
           metadata={"action":action,"channel":channel,"language":language,
                     "scheduled_at":sched,"needs_approval":needs_approval,"ev":ev},
           data_type="ESTIMATED")
    if needs_approval:
        _approval_req(conn, merchant_id, case_id, action, amount, ev)
    return {"ok":True,"action":action,"channel":channel,"language":language,
            "scheduled_at":sched,"needs_approval":needs_approval,
            "expected_value":ev,"data_type":"ESTIMATED"}


def _action_for(case, policy):
    s = case.get("scenario_type",""); r = case.get("failure_reason","")
    if s==SCENARIO_MANDATE_RETRY:
        if r=="mandate_revoked": return "escalate"
        if r=="mandate_expired": return "send_reauth_link"
        return "schedule_retry"
    M = {SCENARIO_FAILED_PAYMENT:"create_payment_link",
         SCENARIO_FAILED_SUBSCRIPTION:"send_reauth_link",
         SCENARIO_CHECKOUT_ABANDONMENT:"send_recovery_link",
         SCENARIO_B2B_RECEIVABLE:"send_invoice_reminder",
         SCENARIO_PROMISE_TO_PAY:"send_promise_followup",
         SCENARIO_PAYMENT_DEGRADATION:"alert_degradation"}
    return M.get(s,"send_message")


def _channel_for(case, policy):
    pref = policy.get("preferred_channel","email")
    if case.get("scenario_type")==SCENARIO_B2B_RECEIVABLE: return "email"
    if case.get("priority")=="critical": return "sms"
    return pref


def _next_slot(policy):
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    s,e = int(policy.get("working_hours_start",9)), int(policy.get("working_hours_end",20))
    if s<=now.hour<e: return now.isoformat(timespec="seconds")
    if now.hour<s:
        t = now.replace(hour=s,minute=0,second=0,microsecond=0)
    else:
        t = (now+timedelta(days=1)).replace(hour=s,minute=0,second=0,microsecond=0)
    return t.isoformat(timespec="seconds")


def _policy(conn, merchant_id):
    r = conn.execute("SELECT * FROM merchant_recovery_policies WHERE merchant_id=?",
                     (merchant_id,)).fetchone()
    if r: return dict(r)
    return {"max_retries":3,"retry_cooldown_hours":24,"max_messages_per_week":3,
            "preferred_channel":"email","preferred_language":"en",
            "working_hours_start":9,"working_hours_end":20,
            "min_expected_value_rs":0,"approval_threshold_rs":10000}


def _approval_req(conn, merchant_id, case_id, action, amount, ev):
    req_id = str(uuid.uuid4())
    exp = (datetime.now(timezone.utc)+timedelta(hours=48)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO approval_requests "
        "(request_id,merchant_id,case_id,action_type,title,description,"
        "recommended_action,expected_value,status,expires_at) VALUES (?,?,?,?,?,?,?,?,'pending',?)",
        (req_id,merchant_id,case_id,action,
         f"Approval required: {action}",
         f"Case {case_id[:8]} Rs{amount:,.0f}. Action: {action}. EV Rs{ev:.2f} [ESTIMATED].",
         action,ev,exp))
    return req_id


# ---- ACT ----

def execute_action(conn, case_id, merchant_id, action_type=None,
                   execution_mode="SIMULATED"):
    """Execute the recovery action. Default mode=SIMULATED."""
    case = get_case(conn, case_id, merchant_id)
    if not case: return {"ok":False,"error":"case_not_found"}
    if case.get("approval_status")=="pending":
        return {"ok":False,"error":"approval_pending",
                "message":"Requires merchant approval before execution."}
    action  = action_type or case.get("recommended_action","send_message")
    channel = case.get("preferred_channel","email")
    amount  = float(case.get("amount") or 0)
    action_id  = str(uuid.uuid4())
    idem_key   = f"{case_id}:{action}:{_NOW()[:10]}"
    try:
        from multilingual import generate_recovery_message
        lang = _policy(conn,merchant_id).get("preferred_language","en")
        msg_result = generate_recovery_message(case, language=lang)
        msg = msg_result.get("message", "") if isinstance(msg_result, dict) else str(msg_result)
    except Exception:
        msg = f"Recovery action for case {case_id[:8]}: {action}"
    details = {"action":action,"channel":channel,"execution_mode":execution_mode,
               "message_preview":msg[:120],"amount_rs":amount}
    try:
        conn.execute(
            "INSERT OR IGNORE INTO recovery_actions "
            "(action_id,case_id,merchant_id,action_type,channel,message_preview,"
            "expected_value,actual_outcome,executed_by,execution_mode,executed_at,"
            "result_details,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (action_id,case_id,merchant_id,action,channel,msg[:200],
             case.get("expected_recovery_value"),"pending","system",
             execution_mode,_NOW(),json.dumps(details),idem_key))
    except Exception as exc:
        log.warning("Action insert failed: %s", exc)
        return {"ok":False,"error":"duplicate_action"}
    cnt = (case.get("communication_count") or 0)+1
    update_case(conn,case_id,merchant_id,
                last_channel_used=channel,last_contacted_at=_NOW(),
                communication_count=cnt, status="in_progress")
    _event(conn,case_id,merchant_id,"action_executed",
           f"Action [{execution_mode}]: {action} via {channel}",
           metadata=details, data_type=execution_mode)
    return {"ok":True,"action_id":action_id,"action":action,
            "channel":channel,"execution_mode":execution_mode,
            "message_preview":msg[:120]}


# ---- OBSERVE ----

def record_outcome(conn, case_id, merchant_id, outcome, realized_value=0.0,
                   actor="system"):
    """Record final outcome. outcome: recovered|failed|escalated."""
    case = get_case(conn, case_id, merchant_id)
    if not case: return {"ok":False,"error":"case_not_found"}
    status_map = {"recovered":"recovered","failed":"failed","escalated":"escalated"}
    new_status = status_map.get(outcome,"failed")
    update_case(conn,case_id,merchant_id,
                status=new_status,realized_value=realized_value,resolved_at=_NOW())
    ev = float(case.get("expected_recovery_value") or 0)
    _event(conn,case_id,merchant_id,f"payment_{outcome}",
           f"Outcome: {outcome}. Realized=Rs{realized_value:.2f} vs Expected=Rs{ev:.2f} [ESTIMATED]",
           actor=actor,
           metadata={"outcome":outcome,"realized_value":realized_value,
                     "expected_value":ev,"delta":round(realized_value-ev,2)},
           data_type="REAL" if realized_value>0 else "SIMULATED")
    try: _feed_learning(conn, case, outcome, realized_value)
    except Exception as exc: log.warning("Learning feed: %s", exc)
    return {"ok":True,"outcome":outcome,"realized_value":realized_value}


def _feed_learning(conn, case, outcome, realized_value):
    import db as _db
    strategy = case.get("selected_strategy") or case.get("recommended_action") or "unknown"
    scenario = case.get("scenario_type","unknown")
    recovered = 1 if outcome=="recovered" else 0
    _db.upsert_strategy_performance(
        conn, strategy=strategy,
        dimension_key="scenario_type", dimension_value=scenario,
        provenance="REAL_TEST" if case.get("source")=="REAL" else "SIMULATION",
        delta_attempts=1, delta_recoveries=recovered,
        delta_amount_recovered=realized_value if recovered else 0,
        delta_amount_attempted=float(case.get("amount") or 0))


# ---- MEASURE ----

def measure_portfolio(conn, merchant_id, is_demo=None):
    """Portfolio-level metrics. Labels each value REAL/ESTIMATED/ACTUAL."""
    cl,pa = ["merchant_id=?"],[merchant_id]
    if is_demo is not None: cl.append("is_demo=?"); pa.append(is_demo)
    rows = conn.execute(f"SELECT * FROM recovery_cases WHERE {' AND '.join(cl)}",pa).fetchall()
    cases = [dict(r) for r in rows]
    total = len(cases)
    recovered = [c for c in cases if c["status"]=="recovered"]
    active    = [c for c in cases if c["status"] in ("open","in_progress","pending_approval")]
    failed    = [c for c in cases if c["status"] in ("failed","escalated")]
    at_risk   = sum(float(c.get("amount_at_risk") or 0) for c in active)
    realized  = sum(float(c.get("realized_value") or 0) for c in recovered)
    expected  = sum(float(c.get("expected_recovery_value") or 0) for c in active)
    rate      = (len(recovered)/total*100) if total>0 else 0.0
    # Checkout
    suffix = " AND is_demo=?" if is_demo is not None else ""
    pco = [merchant_id]+([is_demo] if is_demo is not None else [])
    cr = conn.execute(f"SELECT COUNT(*) n,SUM(amount) tot FROM checkout_sessions WHERE merchant_id=?{suffix}",pco).fetchone()
    # B2B
    ir = conn.execute(f"SELECT COUNT(*) n,SUM(amount) tot FROM b2b_invoices WHERE merchant_id=? AND status NOT IN ('paid','written_off'){suffix}",pco).fetchone()
    # Promises
    pr = conn.execute(f"SELECT COUNT(*) n FROM promises WHERE merchant_id=? AND status='missed'{suffix}",pco).fetchone()
    return {
        "data_types": {"revenue_at_risk":"ACTUAL","recovered_revenue":"ACTUAL",
                       "recovery_rate":"ACTUAL","recoverable_revenue":"ESTIMATED"},
        "total_cases":total, "active_cases":len(active),
        "recovered_cases":len(recovered), "failed_cases":len(failed),
        "revenue_at_risk":round(at_risk,2), "recovered_revenue":round(realized,2),
        "recoverable_revenue":round(expected,2), "recovery_rate":round(rate,1),
        "checkout_abandoned":int(cr["n"] if cr else 0),
        "checkout_value_at_risk":round(float(cr["tot"] or 0) if cr else 0,2),
        "overdue_receivables_count":int(ir["n"] if ir else 0),
        "overdue_receivables_amount":round(float(ir["tot"] or 0) if ir else 0,2),
        "missed_promises":int(pr["n"] if pr else 0),
    }


# ---- Priority queue for command center ----

def priority_queue(conn, merchant_id, limit=20, is_demo=None):
    cl = ["merchant_id=?","status IN ('open','in_progress','pending_approval')"]
    pa = [merchant_id]
    if is_demo is not None: cl.append("is_demo=?"); pa.append(is_demo)
    rows = conn.execute(
        f"SELECT * FROM recovery_cases WHERE {' AND '.join(cl)} "
        "ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        "WHEN 'medium' THEN 2 ELSE 3 END, amount DESC, created_at ASC LIMIT ?",
        pa+[limit]).fetchall()
    cases = [dict(r) for r in rows]
    for c in cases:
        c["what_happened"]  = _what_happened(c)
        c["why_it_matters"] = _why_matters(c)
        c["what_next"]      = _what_next(c)
    return cases


def _what_happened(case):
    s = case.get("scenario_type",""); a = float(case.get("amount") or 0)
    r = case.get("failure_reason") or ""
    M = {SCENARIO_FAILED_PAYMENT:       f"Payment Rs{a:,.0f} failed{' ('+r+')' if r else ''}",
         SCENARIO_FAILED_SUBSCRIPTION:  f"Subscription payment Rs{a:,.0f} failed",
         SCENARIO_MANDATE_RETRY:        f"Mandate retry needed Rs{a:,.0f}",
         SCENARIO_CHECKOUT_ABANDONMENT: f"Checkout abandoned Rs{a:,.0f}",
         SCENARIO_B2B_RECEIVABLE:       f"Invoice Rs{a:,.0f} overdue",
         SCENARIO_PROMISE_TO_PAY:       f"Promise due Rs{a:,.0f}",
         SCENARIO_PAYMENT_DEGRADATION:  f"Payment degradation Rs{a:,.0f} at risk"}
    return M.get(s, f"Recovery case Rs{a:,.0f}")


def _why_matters(case):
    ev   = float(case.get("expected_recovery_value") or 0)
    prob = float(case.get("recovery_probability") or 0)
    return f"Rs{ev:,.0f} recoverable [ESTIMATED]. P(recovery)={prob*100:.0f}% [model estimate]."


def _what_next(case):
    action  = case.get("recommended_action") or "pending analysis"
    channel = case.get("preferred_channel") or "email"
    if case.get("approval_status")=="pending":
        return f"Awaiting approval before: {action}"
    return f"Next: {action} via {channel}"
