
"""Demo Engine - Phase 7.
Deterministic, isolated demo mode. Never contaminates real merchant data.
Creates a complete 10-step revenue recovery narrative for judge demonstrations.
"""
import json, logging, uuid
from datetime import datetime, timezone, timedelta
import db
import recovery_orchestrator as orch
import checkout_recovery
import b2b_recovery
import promise_tracker

log = logging.getLogger("mandate_rescue.demo")
_NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")

DEMO_MERCHANT_ID = "DEMO_MERCHANT_00000000"
DEMO_MARKER = 1  # is_demo=1


def get_or_create_demo_merchant(conn):
    """Get the demo merchant. Creates a synthetic one if not present."""
    r = conn.execute("SELECT * FROM merchants WHERE merchant_id=?",
                     (DEMO_MERCHANT_ID,)).fetchone()
    if r: return dict(r)
    now = _NOW()
    conn.execute(
        """INSERT OR IGNORE INTO merchants
           (merchant_id,email,email_verified,password_hash,full_name,
            business_name,role,is_active,created_at,updated_at,terms_accepted)
           VALUES (?,?,1,'demo_hash','Demo Merchant','Demo Business Inc',
                   'merchant',1,?,?,1)""",
        (DEMO_MERCHANT_ID,"demo@mandaterescue.demo",now,now))
    conn.commit()
    return {"merchant_id":DEMO_MERCHANT_ID,"business_name":"Demo Business Inc",
            "email":"demo@mandaterescue.demo"}


def reset_demo(conn):
    """Clear all demo data for a fresh demo. Never touches real data."""
    tables_with_demo = [
        "recovery_cases","recovery_case_events","checkout_sessions",
        "b2b_invoices","promises","recovery_actions","channel_decisions",
        "voice_scripts","mandate_retry_log","payment_degradation_events",
        "approval_requests",
    ]
    for t in tables_with_demo:
        try: conn.execute(f"DELETE FROM {t} WHERE is_demo=1")
        except Exception:
            try: conn.execute(f"DELETE FROM {t} WHERE merchant_id=?", (DEMO_MERCHANT_ID,))
            except Exception: pass
    conn.commit()
    return {"ok":True,"message":"Demo data cleared"}


def run_full_demo(conn):
    """
    Run the full 10-step demo scenario.
    Returns a structured timeline that can power the demo UI.
    All data is clearly marked SIMULATED.
    """
    get_or_create_demo_merchant(conn)
    reset_demo(conn)
    mid = DEMO_MERCHANT_ID
    steps = []

    # Step 1: Merchant logs in / dashboard loads
    portfolio_before = orch.measure_portfolio(conn, mid, is_demo=1)
    steps.append(_step(1,"Merchant logs in",
        "Dashboard shows zero revenue at risk. System is monitoring.",
        {"portfolio":portfolio_before}))

    # Step 2: Payment failure event arrives
    case_id_1 = orch.create_case(
        conn, mid, orch.SCENARIO_FAILED_PAYMENT,
        amount=28500, failure_reason="insufficient_funds",
        customer_ref="CUST-001", customer_name="Ramesh Gupta",
        customer_email="ramesh@example.com", merchant_category="subscription",
        source="SIMULATED", is_demo=1)
    conn.commit()
    steps.append(_step(2,"Subscription payment failure detected",
        "Rs 28,500 payment failed: insufficient_funds. Case created.",
        {"case_id":case_id_1,"amount":28500,"reason":"insufficient_funds"}))

    # Step 3: Risk scored + root cause
    risk_result = orch.detect_and_score(conn, case_id_1, mid)
    conn.commit()
    steps.append(_step(3,"Risk scored and root cause identified",
        f"Risk score: {risk_result['risk_score']}/100. "
        f"P(recovery): {risk_result['recovery_probability']*100:.0f}% [ESTIMATED]. "
        f"Root cause: Likely insufficient balance at debit time.",
        {"risk":risk_result}))

    # Step 4: AI selects recovery strategy
    decision = orch.decide_action(conn, case_id_1, mid)
    conn.commit()
    steps.append(_step(4,"Recovery strategy selected",
        f"Action: {decision.get('action')}. Channel: {decision.get('channel')}. "
        f"EV: Rs {decision.get('expected_value',0):,.2f} [ESTIMATED].",
        {"decision":decision}))

    # Step 5: Recovery action executed
    action_result = orch.execute_action(conn, case_id_1, mid,
                                         execution_mode="SIMULATED")
    conn.commit()
    preview = action_result.get("message_preview","")
    ch = action_result.get("channel","")
    steps.append(_step(5,"Recovery action executed [SIMULATED]",
        f"Message sent via {ch}: {preview}",
        {"action":action_result,"execution_mode":"SIMULATED"}))

    # Step 6: Checkout abandonment detected
    co_sid, co_cid = checkout_recovery.register_abandonment(
        conn, mid, amount=12000, stage_reached="payment_attempted",
        customer_email="priya@example.com", is_demo=1)
    conn.commit()
    steps.append(_step(6,"Checkout abandonment detected",
        "Customer left checkout at Rs 12,000 — payment_attempted stage.",
        {"session_id":co_sid,"case_id":co_cid,"amount":12000}))

    # Step 7: B2B invoice overdue
    from datetime import timedelta
    past_due = (datetime.now(timezone.utc)-timedelta(days=22)).isoformat()
    inv_id, inv_cid = b2b_recovery.create_invoice(
        conn, mid, customer_name="Acme Corp", amount=85000,
        due_at=past_due, customer_email="acme@example.com",
        invoice_number="INV-DEMO-001", is_demo=1)
    conn.commit()
    steps.append(_step(7,"B2B invoice overdue",
        "Invoice INV-DEMO-001 from Acme Corp: Rs 85,000 — 22 days overdue.",
        {"invoice_id":inv_id,"case_id":inv_cid,"amount":85000}))

    # Step 8: Payment recovered
    outcome = orch.record_outcome(conn, case_id_1, mid, "recovered",
                                   realized_value=28500)
    conn.commit()
    steps.append(_step(8,"Payment recovered",
        "Rs 28,500 recovered! Original case closed. Learning updated.",
        {"case_id":case_id_1,"realized_value":28500,"data_type":"SIMULATED"}))

    # Step 9: Analytics update
    portfolio_after = orch.measure_portfolio(conn, mid, is_demo=1)
    steps.append(_step(9,"Analytics updated",
        f"Recovered: Rs {portfolio_after['recovered_revenue']:,.2f}. "
        f"Still at risk: Rs {portfolio_after['revenue_at_risk']:,.2f}. "
        f"Recovery rate: {portfolio_after['recovery_rate']}%.",
        {"portfolio":portfolio_after}))

    # Step 10: Copilot explains
    explanation = (
        "The system detected a payment failure (insufficient_funds), scored it "
        f"at risk {risk_result['risk_score']:.0f}/100, estimated "
        f"{risk_result['recovery_probability']*100:.0f}% recovery probability, "
        "selected salary-window retry via email, and the customer completed payment. "
        "All values are SIMULATED for demonstration purposes. "
        "In production, Razorpay Real Test Mode webhooks drive real events."
    )
    steps.append(_step(10,"Copilot explains outcome",explanation,
        {"explanation":explanation}))

    return {
        "ok":True,
        "merchant_id":mid,
        "demo_steps":steps,
        "total_steps":len(steps),
        "data_type":"SIMULATED",
        "isolation_note":"All demo data is isolated (is_demo=1). Real merchant data is unaffected.",
        "portfolio_summary":portfolio_after,
    }


def _step(number, title, description, data=None):
    return {
        "step": number, "title": title, "description": description,
        "data": data or {}, "timestamp": _NOW(),
        "data_type": "SIMULATED",
    }


def get_demo_state(conn):
    """Current state of demo data for the UI."""
    mid = DEMO_MERCHANT_ID
    return {
        "portfolio": orch.measure_portfolio(conn, mid, is_demo=1),
        "priority_queue": orch.priority_queue(conn, mid, limit=10, is_demo=1),
        "merchant_id": mid,
        "data_type":"SIMULATED",
    }
