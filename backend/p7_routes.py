
# Phase 7 routes — registered in app.py via register_p7_routes()
import json
import recovery_orchestrator as _orch
import checkout_recovery as _co
import b2b_recovery as _b2b
import promise_tracker as _prom
import channel_engine as _chan
import multilingual as _ml
import mandate_sequencer as _seq
import degradation_investigator as _inv
import demo_engine as _demo
import policy_center as _pol


def register_p7_routes(app, db, _require_auth, jsonify, request):

    @app.route("/api/v2/portfolio")
    def api_v2_portfolio():
        merchant, err = _require_auth()
        if err: return err
        is_demo = request.args.get("demo","0") == "1"
        conn = db.get_connection()
        try:
            return jsonify({"ok":True,"data":_orch.measure_portfolio(conn,merchant["merchant_id"],is_demo=int(is_demo))})
        finally: conn.close()

    @app.route("/api/v2/priority-queue")
    def api_v2_priority_queue():
        merchant, err = _require_auth()
        if err: return err
        is_demo = request.args.get("demo","0") == "1"
        limit = min(int(request.args.get("limit","20")),100)
        conn = db.get_connection()
        try:
            data = _orch.priority_queue(conn,merchant["merchant_id"],limit=limit,is_demo=int(is_demo))
            return jsonify({"ok":True,"data":data,"count":len(data)})
        finally: conn.close()

    @app.route("/api/v2/cases")
    def api_v2_cases():
        merchant, err = _require_auth()
        if err: return err
        mid = merchant["merchant_id"]
        is_demo = request.args.get("demo","0") == "1"
        limit = min(int(request.args.get("limit","50")),200)
        conn = db.get_connection()
        try:
            cases = _orch.get_cases(conn,mid,
                status=request.args.get("status"),
                scenario_type=request.args.get("scenario_type"),
                is_demo=int(is_demo),
                limit=limit,offset=int(request.args.get("offset","0")))
            return jsonify({"ok":True,"data":cases,"count":len(cases)})
        finally: conn.close()

    @app.route("/api/v2/cases/<case_id>")
    def api_v2_case_detail(case_id):
        merchant, err = _require_auth()
        if err: return err
        mid = merchant["merchant_id"]
        conn = db.get_connection()
        try:
            case = _orch.get_case(conn,case_id,mid)
            if not case: return jsonify({"ok":False,"error":"not_found"}),404
            return jsonify({"ok":True,"case":case,"timeline":_orch.get_timeline(conn,case_id,mid)})
        finally: conn.close()

    @app.route("/api/v2/cases",methods=["POST"])
    def api_v2_case_create():
        merchant, err = _require_auth()
        if err: return err
        mid = merchant["merchant_id"]
        data = request.get_json(force=True) or {}
        scenario = data.get("scenario_type")
        if not scenario or scenario not in _orch.ALL_SCENARIOS:
            return jsonify({"ok":False,"error":"invalid_scenario","valid":_orch.ALL_SCENARIOS}),400
        conn = db.get_connection()
        try:
            kw = {k:v for k,v in data.items() if k!="scenario_type"}
            case_id = _orch.create_case(conn,mid,scenario,**kw)
            _orch.detect_and_score(conn,case_id,mid)
            _orch.decide_action(conn,case_id,mid)
            conn.commit()
            return jsonify({"ok":True,"case_id":case_id}),201
        finally: conn.close()

    @app.route("/api/v2/cases/<case_id>/score",methods=["POST"])
    def api_v2_case_score(case_id):
        merchant, err = _require_auth()
        if err: return err
        conn = db.get_connection()
        try:
            r = _orch.detect_and_score(conn,case_id,merchant["merchant_id"])
            conn.commit()
            return jsonify(r)
        finally: conn.close()

    @app.route("/api/v2/cases/<case_id>/decide",methods=["POST"])
    def api_v2_case_decide(case_id):
        merchant, err = _require_auth()
        if err: return err
        conn = db.get_connection()
        try:
            r = _orch.decide_action(conn,case_id,merchant["merchant_id"])
            conn.commit()
            return jsonify(r)
        finally: conn.close()

    @app.route("/api/v2/cases/<case_id>/execute",methods=["POST"])
    def api_v2_case_execute(case_id):
        merchant, err = _require_auth()
        if err: return err
        data = request.get_json(force=True) or {}
        conn = db.get_connection()
        try:
            r = _orch.execute_action(conn,case_id,merchant["merchant_id"],
                action_type=data.get("action_type"),
                execution_mode=data.get("execution_mode","SIMULATED"))
            conn.commit()
            return jsonify(r)
        finally: conn.close()

    @app.route("/api/v2/cases/<case_id>/outcome",methods=["POST"])
    def api_v2_case_outcome(case_id):
        merchant, err = _require_auth()
        if err: return err
        data = request.get_json(force=True) or {}
        outcome = data.get("outcome")
        if outcome not in ("recovered","failed","escalated"):
            return jsonify({"ok":False,"error":"outcome must be recovered|failed|escalated"}),400
        conn = db.get_connection()
        try:
            r = _orch.record_outcome(conn,case_id,merchant["merchant_id"],
                outcome,float(data.get("realized_value",0)),actor=merchant["merchant_id"])
            conn.commit()
            return jsonify(r)
        finally: conn.close()

    @app.route("/api/v2/cases/<case_id>/timeline")
    def api_v2_case_timeline(case_id):
        merchant, err = _require_auth()
        if err: return err
        conn = db.get_connection()
        try:
            tl = _orch.get_timeline(conn,case_id,merchant["merchant_id"])
            return jsonify({"ok":True,"timeline":tl,"count":len(tl)})
        finally: conn.close()

    @app.route("/api/v2/cases/<case_id>/message")
    def api_v2_case_message(case_id):
        merchant, err = _require_auth()
        if err: return err
        conn = db.get_connection()
        try:
            case = _orch.get_case(conn,case_id,merchant["merchant_id"])
            if not case: return jsonify({"ok":False,"error":"not_found"}),404
            return jsonify({"ok":True,"messages":_ml.generate_all_languages(case),"case_id":case_id})
        finally: conn.close()

    @app.route("/api/v2/cases/<case_id>/voice-script",methods=["POST"])
    def api_v2_case_voice_script(case_id):
        merchant, err = _require_auth()
        if err: return err
        data = request.get_json(force=True) or {}
        conn = db.get_connection()
        try:
            r = _chan.create_voice_script(conn,case_id,merchant["merchant_id"],
                language=data.get("language","en"),
                call_intent=data.get("call_intent","recovery_reminder"))
            conn.commit()
            return jsonify(r)
        finally: conn.close()

    @app.route("/api/v2/checkout/sessions")
    def api_v2_checkout_sessions():
        merchant, err = _require_auth()
        if err: return err
        is_demo = request.args.get("demo","0") == "1"
        conn = db.get_connection()
        try:
            ss = _co.get_abandoned_sessions(conn,merchant["merchant_id"],is_demo=int(is_demo))
            return jsonify({"ok":True,"data":ss,"count":len(ss)})
        finally: conn.close()

    @app.route("/api/v2/checkout/sessions",methods=["POST"])
    def api_v2_checkout_register():
        merchant, err = _require_auth()
        if err: return err
        data = request.get_json(force=True) or {}
        amount = float(data.get("amount",0))
        if amount <= 0: return jsonify({"ok":False,"error":"amount required"}),400
        conn = db.get_connection()
        try:
            sid,cid = _co.register_abandonment(conn,merchant["merchant_id"],
                amount=amount,stage_reached=data.get("stage_reached","abandoned"),
                customer_email=data.get("customer_email"),
                customer_phone=data.get("customer_phone"),
                customer_ref=data.get("customer_ref"),
                payment_method=data.get("payment_method"),
                is_demo=int(data.get("is_demo",0)))
            conn.commit()
            return jsonify({"ok":True,"session_id":sid,"case_id":cid}),201
        finally: conn.close()

    @app.route("/api/v2/checkout/funnel")
    def api_v2_checkout_funnel():
        merchant, err = _require_auth()
        if err: return err
        is_demo = request.args.get("demo","0") == "1"
        conn = db.get_connection()
        try:
            return jsonify({"ok":True,"data":_co.recovery_funnel(conn,merchant["merchant_id"],is_demo=int(is_demo))})
        finally: conn.close()

    @app.route("/api/v2/checkout/sessions/<session_id>/recover",methods=["POST"])
    def api_v2_checkout_recover(session_id):
        merchant, err = _require_auth()
        if err: return err
        data = request.get_json(force=True) or {}
        conn = db.get_connection()
        try:
            r = _co.mark_recovered(conn,merchant["merchant_id"],session_id,
                realized_value=data.get("realized_value"))
            conn.commit()
            return jsonify(r)
        finally: conn.close()

    @app.route("/api/v2/b2b/invoices")
    def api_v2_b2b_invoices():
        merchant, err = _require_auth()
        if err: return err
        is_demo = request.args.get("demo","0") == "1"
        conn = db.get_connection()
        try:
            invs = _b2b.get_invoices(conn,merchant["merchant_id"],
                status=request.args.get("status"),is_demo=int(is_demo))
            return jsonify({"ok":True,"data":invs,"count":len(invs)})
        finally: conn.close()

    @app.route("/api/v2/b2b/invoices",methods=["POST"])
    def api_v2_b2b_create():
        merchant, err = _require_auth()
        if err: return err
        data = request.get_json(force=True) or {}
        for f in ["customer_name","amount","due_at"]:
            if not data.get(f): return jsonify({"ok":False,"error":f"{f} required"}),400
        conn = db.get_connection()
        try:
            inv_id,cid = _b2b.create_invoice(conn,merchant["merchant_id"],
                customer_name=data["customer_name"],amount=float(data["amount"]),
                due_at=data["due_at"],customer_email=data.get("customer_email"),
                customer_phone=data.get("customer_phone"),
                customer_company=data.get("customer_company"),
                invoice_number=data.get("invoice_number"),
                is_demo=int(data.get("is_demo",0)))
            conn.commit()
            return jsonify({"ok":True,"invoice_id":inv_id,"case_id":cid}),201
        finally: conn.close()

    @app.route("/api/v2/b2b/invoices/<invoice_id>/remind",methods=["POST"])
    def api_v2_b2b_remind(invoice_id):
        merchant, err = _require_auth()
        if err: return err
        conn = db.get_connection()
        try:
            r = _b2b.send_reminder(conn,merchant["merchant_id"],invoice_id)
            conn.commit(); return jsonify(r)
        finally: conn.close()

    @app.route("/api/v2/b2b/invoices/<invoice_id>/escalate",methods=["POST"])
    def api_v2_b2b_escalate(invoice_id):
        merchant, err = _require_auth()
        if err: return err
        data = request.get_json(force=True) or {}
        conn = db.get_connection()
        try:
            r = _b2b.escalate_invoice(conn,merchant["merchant_id"],invoice_id,notes=data.get("notes"))
            conn.commit(); return jsonify(r)
        finally: conn.close()

    @app.route("/api/v2/b2b/invoices/<invoice_id>/paid",methods=["POST"])
    def api_v2_b2b_paid(invoice_id):
        merchant, err = _require_auth()
        if err: return err
        data = request.get_json(force=True) or {}
        conn = db.get_connection()
        try:
            r = _b2b.mark_paid(conn,merchant["merchant_id"],invoice_id,paid_amount=data.get("paid_amount"))
            conn.commit(); return jsonify(r)
        finally: conn.close()

    @app.route("/api/v2/b2b/aging")
    def api_v2_b2b_aging():
        merchant, err = _require_auth()
        if err: return err
        is_demo = request.args.get("demo","0") == "1"
        conn = db.get_connection()
        try:
            return jsonify({"ok":True,"data":_b2b.aging_summary(conn,merchant["merchant_id"],is_demo=int(is_demo))})
        finally: conn.close()

    @app.route("/api/v2/promises")
    def api_v2_promises():
        merchant, err = _require_auth()
        if err: return err
        is_demo = request.args.get("demo","0") == "1"
        conn = db.get_connection()
        try:
            _prom.refresh_statuses(conn,merchant["merchant_id"])
            ps = _prom.get_promises(conn,merchant["merchant_id"],
                status=request.args.get("status"),is_demo=int(is_demo))
            return jsonify({"ok":True,"data":ps,"count":len(ps)})
        finally: conn.close()

    @app.route("/api/v2/promises",methods=["POST"])
    def api_v2_promise_create():
        merchant, err = _require_auth()
        if err: return err
        data = request.get_json(force=True) or {}
        for f in ["promised_amount","promised_date"]:
            if not data.get(f): return jsonify({"ok":False,"error":f"{f} required"}),400
        conn = db.get_connection()
        try:
            pid = _prom.create_promise(conn,merchant["merchant_id"],
                float(data["promised_amount"]),data["promised_date"],
                customer_ref=data.get("customer_ref"),customer_name=data.get("customer_name"),
                customer_email=data.get("customer_email"),case_id=data.get("case_id"),
                invoice_id=data.get("invoice_id"),source=data.get("source","manual"),
                confidence=data.get("confidence","medium"),notes=data.get("notes"),
                is_demo=int(data.get("is_demo",0)))
            conn.commit()
            return jsonify({"ok":True,"promise_id":pid}),201
        finally: conn.close()

    @app.route("/api/v2/promises/<promise_id>/paid",methods=["POST"])
    def api_v2_promise_paid(promise_id):
        merchant, err = _require_auth()
        if err: return err
        data = request.get_json(force=True) or {}
        conn = db.get_connection()
        try:
            r = _prom.mark_paid(conn,merchant["merchant_id"],promise_id,actual_amount=data.get("actual_amount"))
            conn.commit(); return jsonify(r)
        finally: conn.close()

    @app.route("/api/v2/promises/<promise_id>/missed",methods=["POST"])
    def api_v2_promise_missed(promise_id):
        merchant, err = _require_auth()
        if err: return err
        conn = db.get_connection()
        try:
            r = _prom.mark_missed(conn,merchant["merchant_id"],promise_id)
            conn.commit(); return jsonify(r)
        finally: conn.close()

    @app.route("/api/v2/promises/summary")
    def api_v2_promises_summary():
        merchant, err = _require_auth()
        if err: return err
        is_demo = request.args.get("demo","0") == "1"
        conn = db.get_connection()
        try:
            return jsonify({"ok":True,"data":_prom.summary(conn,merchant["merchant_id"],is_demo=int(is_demo))})
        finally: conn.close()

    @app.route("/api/v2/approvals")
    def api_v2_approvals():
        merchant, err = _require_auth()
        if err: return err
        status = request.args.get("status","pending")
        conn = db.get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM approval_requests WHERE merchant_id=? AND status=? ORDER BY created_at DESC LIMIT 50",
                (merchant["merchant_id"],status)).fetchall()
            return jsonify({"ok":True,"data":[dict(r) for r in rows]})
        finally: conn.close()

    @app.route("/api/v2/approvals/<request_id>",methods=["POST"])
    def api_v2_approval_decide(request_id):
        merchant, err = _require_auth()
        if err: return err
        mid = merchant["merchant_id"]
        data = request.get_json(force=True) or {}
        decision = data.get("decision")
        if decision not in ("approved","rejected"):
            return jsonify({"ok":False,"error":"decision: approved|rejected"}),400
        conn = db.get_connection()
        try:
            r = conn.execute(
                "SELECT * FROM approval_requests WHERE request_id=? AND merchant_id=?",
                (request_id,mid)).fetchone()
            if not r: return jsonify({"ok":False,"error":"not_found"}),404
            r = dict(r)
            if r["status"] != "pending":
                return jsonify({"ok":False,"error":f"Already {r['status']}"}),409
            conn.execute(
                "UPDATE approval_requests SET status=?,decided_by=?,decided_at=?,decision_notes=? WHERE request_id=?",
                (decision,mid,_orch._NOW(),data.get("notes"),request_id))
            if decision=="approved" and r.get("case_id"):
                _orch.update_case(conn,r["case_id"],mid,approval_status="approved",
                                  approved_by=mid,status="in_progress")
                _orch.execute_action(conn,r["case_id"],mid,
                                     action_type=r.get("action_type"),execution_mode="SIMULATED")
            elif decision=="rejected" and r.get("case_id"):
                _orch.update_case(conn,r["case_id"],mid,approval_status="rejected")
            conn.commit()
            return jsonify({"ok":True,"request_id":request_id,"decision":decision})
        finally: conn.close()

    @app.route("/api/v2/policy")
    def api_v2_policy_get():
        merchant, err = _require_auth()
        if err: return err
        conn = db.get_connection()
        try:
            return jsonify({"ok":True,"policy":_pol.get_merchant_policy(conn,merchant["merchant_id"])})
        finally: conn.close()

    @app.route("/api/v2/policy",methods=["PATCH"])
    def api_v2_policy_update():
        merchant, err = _require_auth()
        if err: return err
        data = request.get_json(force=True) or {}
        conn = db.get_connection()
        try:
            result = _pol.upsert_policy(conn,merchant["merchant_id"],**data)
            return jsonify(result),(200 if result.get("ok") else 400)
        finally: conn.close()

    @app.route("/api/v2/policy/reset",methods=["POST"])
    def api_v2_policy_reset():
        merchant, err = _require_auth()
        if err: return err
        conn = db.get_connection()
        try:
            return jsonify(_pol.reset_to_defaults(conn,merchant["merchant_id"]))
        finally: conn.close()

    @app.route("/api/v2/mandate-retry/schedule",methods=["POST"])
    def api_v2_mandate_retry_schedule():
        merchant, err = _require_auth()
        if err: return err
        data = request.get_json(force=True) or {}
        cid = data.get("mandate_customer_id")
        if not cid: return jsonify({"ok":False,"error":"mandate_customer_id required"}),400
        conn = db.get_connection()
        try:
            r = _seq.schedule_mandate_retry(conn,merchant["merchant_id"],cid,
                case_id=data.get("case_id"),
                execution_mode=data.get("execution_mode","SIMULATED"))
            conn.commit(); return jsonify(r)
        finally: conn.close()

    @app.route("/api/v2/mandate-retry/history")
    def api_v2_mandate_retry_history():
        merchant, err = _require_auth()
        if err: return err
        conn = db.get_connection()
        try:
            hist = _seq.get_retry_history(conn,merchant["merchant_id"],
                case_id=request.args.get("case_id"),
                mandate_customer_id=request.args.get("customer_id"))
            return jsonify({"ok":True,"data":hist,"count":len(hist)})
        finally: conn.close()

    @app.route("/api/v2/investigate")
    def api_v2_investigate():
        merchant, err = _require_auth()
        if err: return err
        is_demo = request.args.get("demo","0") == "1"
        conn = db.get_connection()
        try:
            return jsonify(_inv.investigate(conn,merchant["merchant_id"],
                question=request.args.get("q"),is_demo=int(is_demo)))
        finally: conn.close()

    @app.route("/api/v2/degradation/events")
    def api_v2_degradation_events():
        merchant, err = _require_auth()
        if err: return err
        is_demo = request.args.get("demo","0") == "1"
        conn = db.get_connection()
        try:
            events = _inv.get_degradation_events(conn,merchant["merchant_id"],is_demo=int(is_demo))
            return jsonify({"ok":True,"data":events,"count":len(events)})
        finally: conn.close()

    @app.route("/api/v2/channel/select",methods=["POST"])
    def api_v2_channel_select():
        merchant, err = _require_auth()
        if err: return err
        data = request.get_json(force=True) or {}
        case_id = data.get("case_id")
        conn = db.get_connection()
        try:
            policy = _pol.get_merchant_policy(conn,merchant["merchant_id"])
            case = _orch.get_case(conn,case_id,merchant["merchant_id"]) if case_id else data.get("case",{})
            if case_id and not case: return jsonify({"ok":False,"error":"case not found"}),404
            result = _chan.select_channel(case,policy)
            if case_id:
                _chan.record_channel_decision(conn,case_id,merchant["merchant_id"],result)
                conn.commit()
            return jsonify({"ok":True,"data":result})
        finally: conn.close()

    @app.route("/api/v2/demo/run",methods=["POST"])
    def api_v2_demo_run():
        merchant, err = _require_auth()
        if err: return err
        conn = db.get_connection()
        try:
            result = _demo.run_full_demo(conn)
            conn.commit(); return jsonify(result)
        finally: conn.close()

    @app.route("/api/v2/demo/state")
    def api_v2_demo_state():
        merchant, err = _require_auth()
        if err: return err
        conn = db.get_connection()
        try:
            return jsonify({"ok":True,"data":_demo.get_demo_state(conn)})
        finally: conn.close()

    @app.route("/api/v2/demo/reset",methods=["POST"])
    def api_v2_demo_reset():
        merchant, err = _require_auth()
        if err: return err
        conn = db.get_connection()
        try:
            r = _demo.reset_demo(conn); conn.commit(); return jsonify(r)
        finally: conn.close()

    @app.route("/api/v2/demo/seed-checkouts",methods=["POST"])
    def api_v2_demo_seed_checkouts():
        merchant, err = _require_auth()
        if err: return err
        mid = merchant["merchant_id"]
        conn = db.get_connection()
        try:
            r1 = _co.seed_demo_checkouts(conn,mid)
            r2 = _b2b.seed_demo_invoices(conn,mid)
            r3 = _prom.seed_demo_promises(conn,mid)
            conn.commit()
            return jsonify({"ok":True,"checkouts":len(r1),"invoices":len(r2),
                            "promises":len(r3),"data_type":"SIMULATED"})
        finally: conn.close()

    @app.route("/api/v2/analytics/recovery-funnel")
    def api_v2_analytics_funnel():
        merchant, err = _require_auth()
        if err: return err
        mid = merchant["merchant_id"]
        is_demo = request.args.get("demo","0") == "1"
        conn = db.get_connection()
        try:
            return jsonify({
                "ok":True,
                "portfolio":_orch.measure_portfolio(conn,mid,is_demo=int(is_demo)),
                "checkout_funnel":_co.recovery_funnel(conn,mid,is_demo=int(is_demo)),
                "b2b_aging":_b2b.aging_summary(conn,mid,is_demo=int(is_demo)),
                "promises":_prom.summary(conn,mid,is_demo=int(is_demo)),
                "data_type":"ACTUAL",
            })
        finally: conn.close()

    @app.route("/api/v2/analytics/strategy-performance")
    def api_v2_analytics_strategy():
        merchant, err = _require_auth()
        if err: return err
        conn = db.get_connection()
        try:
            rows = db.get_strategy_performance(conn)
            return jsonify({"ok":True,"data":rows,"count":len(rows)})
        finally: conn.close()

    @app.route("/api/v2/revenue-journey")
    def api_v2_revenue_journey():
        merchant, err = _require_auth()
        if err: return err
        mid = merchant["merchant_id"]
        is_demo = request.args.get("demo","0") == "1"
        conn = db.get_connection()
        try:
            p = _orch.measure_portfolio(conn,mid,is_demo=int(is_demo))
            stages = [
                {"stage":"Payment Intent","count":p["total_cases"],"value_rs":p["revenue_at_risk"]+p["recovered_revenue"]},
                {"stage":"Checkout","count":p["checkout_abandoned"],"value_rs":p["checkout_value_at_risk"]},
                {"stage":"Payment","count":p["total_cases"],"value_rs":p["revenue_at_risk"]+p["recovered_revenue"]},
                {"stage":"Failure / Risk","count":p["active_cases"],"value_rs":p["revenue_at_risk"]},
                {"stage":"Recovery","count":p["active_cases"],"value_rs":p["recoverable_revenue"]},
                {"stage":"Recovered","count":p["recovered_cases"],"value_rs":p["recovered_revenue"]},
                {"stage":"Overdue Receivables","count":p["overdue_receivables_count"],"value_rs":p["overdue_receivables_amount"]},
                {"stage":"Missed Promises","count":p["missed_promises"],"value_rs":0},
            ]
            return jsonify({"ok":True,"stages":stages,"portfolio":p,
                            "data_type":"ACTUAL (counts) / ESTIMATED (expected values)"})
        finally: conn.close()

    @app.route("/api/v2/copilot/ask",methods=["POST"])
    def api_v2_copilot_ask():
        merchant, err = _require_auth()
        if err: return err
        mid = merchant["merchant_id"]
        data = request.get_json(force=True) or {}
        q = (data.get("question") or "").strip()
        if not q: return jsonify({"ok":False,"error":"question required"}),400
        conn = db.get_connection()
        try:
            answer = _copilot_answer(conn,mid,q)
            return jsonify({"ok":True,"answer":answer,"question":q})
        finally: conn.close()

    def _copilot_answer(conn, merchant_id, question):
        portfolio = _orch.measure_portfolio(conn,merchant_id)
        q = question.lower()
        if any(w in q for w in ["at risk","risk","revenue risk"]):
            return (f"Currently Rs {portfolio['revenue_at_risk']:,.2f} is at risk across "
                    f"{portfolio['active_cases']} active cases. Data type: ACTUAL.")
        if any(w in q for w in ["recovery rate","how much recovered","recovered"]):
            return (f"Rs {portfolio['recovered_revenue']:,.2f} has been recovered. "
                    f"Recovery rate: {portfolio['recovery_rate']}%. Data type: ACTUAL.")
        if any(w in q for w in ["failing","failure","why","degraded","payment method"]):
            inv = _inv.investigate(conn,merchant_id,question=question)
            if inv.get("investigations"):
                top = inv["investigations"][0]
                return (f"Top signal: {top['observation']}. Likely cause: {top['likely_cause']}. "
                        f"Revenue impact: Rs {top['revenue_impact_rs']:,.2f}. "
                        f"Recommendation: {top['recommendation']} [confidence: {top['confidence']}]")
            return "Insufficient data to identify a dominant failure pattern. Run the agent to collect more outcomes."
        if any(w in q for w in ["priority","highest","top case","urgent"]):
            queue = _orch.priority_queue(conn,merchant_id,limit=3)
            if not queue: return "No open high-priority cases at this time."
            return "Top priority cases: " + " | ".join(c["what_happened"] for c in queue[:3])
        if any(w in q for w in ["strategy","best strategy","performs"]):
            rows = db.get_strategy_performance(conn)
            if not rows: return "No strategy performance data yet. Run the recovery agent to collect outcomes."
            best = max(rows,key=lambda r: r.get("recoveries",0)/max(r.get("attempts",1),1))
            rate = best.get("recoveries",0)/max(best.get("attempts",1),1)
            return (f"Best observed strategy: '{best['strategy']}' ({rate*100:.1f}% recovery rate on "
                    f"{best['attempts']} attempts). Provenance: {best.get('provenance','unknown')}.")
        if any(w in q for w in ["checkout","abandoned","drop"]):
            return (f"{portfolio['checkout_abandoned']} abandoned checkouts worth "
                    f"Rs {portfolio['checkout_value_at_risk']:,.2f}. Data type: ACTUAL.")
        if any(w in q for w in ["promise","missed promise"]):
            return (f"{portfolio['missed_promises']} missed payment promises. Data type: ACTUAL.")
        if any(w in q for w in ["overdue","invoice","b2b","receivable"]):
            return (f"Rs {portfolio['overdue_receivables_amount']:,.2f} in overdue receivables across "
                    f"{portfolio['overdue_receivables_count']} invoices. Data type: ACTUAL.")
        if any(w in q for w in ["policy","retry limit","cooldown"]):
            pol = _pol.get_merchant_policy(conn,merchant_id)
            return (f"Current policy: max {pol.get('max_retries',3)} retries, "
                    f"{pol.get('retry_cooldown_hours',24)}h cooldown, "
                    f"channel: {pol.get('preferred_channel','email')}, "
                    f"language: {pol.get('preferred_language','en')}.")
        try:
            import llm_client as _llm
            context = f"Portfolio: {json.dumps(portfolio,default=str)}. Question: {question}"
            resp = _llm.ask(context,max_tokens=200)
            if resp: return resp + " [LLM-generated. Verify against dashboard data.]"
        except Exception: pass
        return (f"I can see {portfolio['total_cases']} cases, Rs {portfolio['revenue_at_risk']:,.2f} at risk, "
                f"Rs {portfolio['recovered_revenue']:,.2f} recovered. Ask about: risk, recovery rate, "
                f"failures, priority cases, strategy, checkout, promises, B2B, or policy.")

    @app.route("/api/v2/messages/generate",methods=["POST"])
    def api_v2_messages_generate():
        merchant, err = _require_auth()
        if err: return err
        data = request.get_json(force=True) or {}
        msg = _ml.generate_recovery_message(data.get("case",{}),
            language=data.get("language","en"),
            recovery_link=data.get("recovery_link"))
        return jsonify({"ok":True,"message":msg})

    @app.route("/api/v2/messages/all-languages",methods=["POST"])
    def api_v2_messages_all_languages():
        merchant, err = _require_auth()
        if err: return err
        data = request.get_json(force=True) or {}
        msgs = _ml.generate_all_languages(data.get("case",{}),
            recovery_link=data.get("recovery_link"))
        return jsonify({"ok":True,"messages":msgs})
