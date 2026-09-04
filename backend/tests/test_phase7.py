
"""Phase 7 test suite."""
import pytest
from datetime import datetime, timezone, timedelta
import db


def _merchant(conn, mid="M-TEST-001"):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO merchants "
            "(merchant_id,email,email_verified,password_hash,full_name,"
            "business_name,role,is_active,created_at,updated_at,terms_accepted) "
            "VALUES (?,?,1,?,?,?,'merchant',1,?,?,1)",
            (mid,f"{mid}@test.com","pbkdf2:sha256:1","Test Merchant","Test Business",now,now))
        conn.commit()
    except Exception:
        pass
    return mid


class TestPhase7Schema:
    def test_all_p7_tables_exist(self, empty_db):
        tables = {r[0] for r in empty_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for t in ["recovery_cases","recovery_case_events","checkout_sessions",
                  "b2b_invoices","promises","recovery_actions","channel_decisions",
                  "voice_scripts","mandate_retry_log","payment_degradation_events",
                  "merchant_recovery_policies","approval_requests"]:
            assert t in tables, f"Missing table: {t}"

    def test_recovery_cases_columns(self, empty_db):
        cols = {r[1] for r in empty_db.execute("PRAGMA table_info(recovery_cases)").fetchall()}
        for c in ["merchant_id","scenario_type","is_demo","amount","status","priority"]:
            assert c in cols

    def test_merchant_isolation_columns(self, empty_db):
        for t in ["b2b_invoices","promises","checkout_sessions"]:
            cols = {r[1] for r in empty_db.execute(f"PRAGMA table_info({t})").fetchall()}
            assert "merchant_id" in cols, f"{t} missing merchant_id"
            assert "is_demo" in cols, f"{t} missing is_demo"

    def test_reset_clears_p7_tables(self, empty_db):
        import recovery_orchestrator as orch
        mid = _merchant(empty_db)
        orch.create_case(empty_db, mid, "failed_payment", amount=100)
        empty_db.commit()
        from phase7_schema import reset_phase7
        reset_phase7(empty_db)
        empty_db.commit()
        n = empty_db.execute("SELECT COUNT(*) FROM recovery_cases").fetchone()[0]
        assert n == 0


class TestOrchestrator:
    def test_create_case(self, empty_db):
        import recovery_orchestrator as o
        mid = _merchant(empty_db)
        cid = o.create_case(empty_db, mid, o.SCENARIO_FAILED_PAYMENT, amount=5000)
        empty_db.commit()
        row = empty_db.execute("SELECT * FROM recovery_cases WHERE case_id=?", (cid,)).fetchone()
        assert row["merchant_id"] == mid
        assert row["scenario_type"] == "failed_payment"
        assert float(row["amount"]) == 5000

    def test_merchant_isolation(self, empty_db):
        import recovery_orchestrator as o
        m1 = _merchant(empty_db, "M-001"); m2 = _merchant(empty_db, "M-002")
        cid1 = o.create_case(empty_db, m1, "failed_payment", amount=1000)
        empty_db.commit()
        assert o.get_case(empty_db, cid1, m2) is None
        assert o.get_case(empty_db, cid1, m1) is not None

    def test_detect_and_score(self, empty_db):
        import recovery_orchestrator as o
        mid = _merchant(empty_db)
        cid = o.create_case(empty_db, mid, "failed_payment", amount=10000,
                            failure_reason="insufficient_funds")
        empty_db.commit()
        r = o.detect_and_score(empty_db, cid, mid)
        assert r["ok"] is True
        assert 0 <= r["risk_score"] <= 100
        assert 0 <= r["recovery_probability"] <= 1
        assert r["data_type"] == "ESTIMATED"

    def test_decide_action(self, empty_db):
        import recovery_orchestrator as o
        mid = _merchant(empty_db)
        cid = o.create_case(empty_db, mid, "failed_payment", amount=500,
                            failure_reason="bank_technical_error")
        empty_db.commit()
        o.detect_and_score(empty_db, cid, mid)
        d = o.decide_action(empty_db, cid, mid)
        assert d["ok"] is True
        assert d["action"]
        assert d["data_type"] == "ESTIMATED"

    def test_high_value_needs_approval(self, empty_db):
        import recovery_orchestrator as o
        mid = _merchant(empty_db)
        cid = o.create_case(empty_db, mid, "b2b_receivable", amount=50000)
        empty_db.commit()
        o.detect_and_score(empty_db, cid, mid)
        d = o.decide_action(empty_db, cid, mid)
        empty_db.commit()
        assert d["needs_approval"] is True
        ar = empty_db.execute("SELECT * FROM approval_requests WHERE case_id=?", (cid,)).fetchone()
        assert ar is not None

    def test_execute_action_blocked_pending_approval(self, empty_db):
        import recovery_orchestrator as o
        mid = _merchant(empty_db)
        cid = o.create_case(empty_db, mid, "b2b_receivable", amount=50000)
        empty_db.commit()
        o.detect_and_score(empty_db, cid, mid)
        o.decide_action(empty_db, cid, mid)
        empty_db.commit()
        r = o.execute_action(empty_db, cid, mid, execution_mode="SIMULATED")
        assert r.get("error") == "approval_pending"

    def test_execute_action_small_amount(self, empty_db):
        import recovery_orchestrator as o
        mid = _merchant(empty_db)
        cid = o.create_case(empty_db, mid, "failed_payment", amount=500,
                            failure_reason="bank_technical_error")
        empty_db.commit()
        o.detect_and_score(empty_db, cid, mid)
        o.decide_action(empty_db, cid, mid)
        empty_db.commit()
        r = o.execute_action(empty_db, cid, mid, execution_mode="SIMULATED")
        assert r.get("ok") is True
        assert r["execution_mode"] == "SIMULATED"

    def test_record_outcome_recovered(self, empty_db):
        import recovery_orchestrator as o
        mid = _merchant(empty_db)
        cid = o.create_case(empty_db, mid, "failed_payment", amount=2000)
        empty_db.commit()
        o.detect_and_score(empty_db, cid, mid)
        o.decide_action(empty_db, cid, mid)
        o.execute_action(empty_db, cid, mid)
        r = o.record_outcome(empty_db, cid, mid, "recovered", 2000.0)
        empty_db.commit()
        assert r["ok"] is True
        row = empty_db.execute("SELECT status,realized_value FROM recovery_cases WHERE case_id=?", (cid,)).fetchone()
        assert row["status"] == "recovered"
        assert float(row["realized_value"]) == 2000.0

    def test_measure_portfolio(self, empty_db):
        import recovery_orchestrator as o
        mid = _merchant(empty_db)
        cid = o.create_case(empty_db, mid, "failed_payment", amount=3000)
        empty_db.commit()
        o.detect_and_score(empty_db, cid, mid)
        m = o.measure_portfolio(empty_db, mid)
        assert m["total_cases"] >= 1
        assert "revenue_at_risk" in m
        assert "data_types" in m

    def test_priority_queue_order(self, empty_db):
        import recovery_orchestrator as o
        mid = _merchant(empty_db)
        o.create_case(empty_db, mid, "failed_payment", amount=100, priority="low")
        o.create_case(empty_db, mid, "failed_payment", amount=50000, priority="critical")
        o.create_case(empty_db, mid, "failed_payment", amount=5000, priority="high")
        empty_db.commit()
        q = o.priority_queue(empty_db, mid)
        assert q[0]["priority"] == "critical"

    def test_timeline_appended(self, empty_db):
        import recovery_orchestrator as o
        mid = _merchant(empty_db)
        cid = o.create_case(empty_db, mid, "failed_payment", amount=1000)
        empty_db.commit()
        o.detect_and_score(empty_db, cid, mid)
        empty_db.commit()
        tl = o.get_timeline(empty_db, cid, mid)
        types = [e["event_type"] for e in tl]
        assert "case_created" in types
        assert "risk_scored" in types

    def test_case_not_found_returns_error(self, empty_db):
        import recovery_orchestrator as o
        mid = _merchant(empty_db)
        r = o.detect_and_score(empty_db, "NONEXISTENT", mid)
        assert r["ok"] is False

    def test_learning_feed_on_outcome(self, empty_db):
        import recovery_orchestrator as o
        mid = _merchant(empty_db)
        cid = o.create_case(empty_db, mid, "failed_payment", amount=5000,
                            failure_reason="bank_technical_error")
        empty_db.commit()
        o.detect_and_score(empty_db, cid, mid)
        o.decide_action(empty_db, cid, mid)
        o.execute_action(empty_db, cid, mid)
        o.record_outcome(empty_db, cid, mid, "recovered", 5000)
        empty_db.commit()
        rows = db.get_strategy_performance(empty_db)
        assert any(r["dimension_key"] == "scenario_type" for r in rows)


class TestCheckoutRecovery:
    def test_register_abandonment_creates_case(self, empty_db):
        import checkout_recovery as co
        mid = _merchant(empty_db)
        sid, cid = co.register_abandonment(empty_db, mid, amount=8000,
                                           stage_reached="payment_attempted",
                                           customer_email="t@test.com", is_demo=1)
        empty_db.commit()
        row = empty_db.execute("SELECT * FROM checkout_sessions WHERE session_id=?", (sid,)).fetchone()
        assert row["merchant_id"] == mid
        assert row["status"] == "abandoned"
        assert row["is_demo"] == 1

    def test_merchant_isolation_checkout(self, empty_db):
        import checkout_recovery as co
        m1 = _merchant(empty_db, "CO-M1"); m2 = _merchant(empty_db, "CO-M2")
        co.register_abandonment(empty_db, m1, amount=5000, is_demo=0)
        empty_db.commit()
        assert len(co.get_abandoned_sessions(empty_db, m2)) == 0

    def test_mark_recovered(self, empty_db):
        import checkout_recovery as co
        mid = _merchant(empty_db)
        sid, cid = co.register_abandonment(empty_db, mid, amount=3000, is_demo=1)
        empty_db.commit()
        r = co.mark_recovered(empty_db, mid, sid, realized_value=3000)
        empty_db.commit()
        assert r["ok"] is True
        row = empty_db.execute("SELECT status FROM checkout_sessions WHERE session_id=?", (sid,)).fetchone()
        assert row["status"] == "recovered"

    def test_funnel_metrics(self, empty_db):
        import checkout_recovery as co
        mid = _merchant(empty_db)
        for _ in range(3):
            co.register_abandonment(empty_db, mid, amount=1000, is_demo=1)
        empty_db.commit()
        f = co.recovery_funnel(empty_db, mid, is_demo=1)
        assert f["abandoned_sessions"] == 3
        assert f["data_type"] == "ACTUAL"

    def test_demo_seed(self, empty_db):
        import checkout_recovery as co
        mid = _merchant(empty_db)
        r = co.seed_demo_checkouts(empty_db, mid)
        empty_db.commit()
        assert len(r) == 5


class TestB2BRecovery:
    def _due(self, days_ago=0):
        return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()

    def test_create_invoice(self, empty_db):
        import b2b_recovery as b2b
        mid = _merchant(empty_db)
        inv_id, cid = b2b.create_invoice(empty_db, mid, "Acme Corp",
                                         amount=50000, due_at=self._due(10), is_demo=1)
        empty_db.commit()
        row = empty_db.execute("SELECT * FROM b2b_invoices WHERE invoice_id=?", (inv_id,)).fetchone()
        assert row["merchant_id"] == mid
        assert row["is_demo"] == 1

    def test_overdue_detection(self, empty_db):
        import b2b_recovery as b2b
        mid = _merchant(empty_db)
        inv_id, _ = b2b.create_invoice(empty_db, mid, "OD Corp",
                                       amount=10000, due_at=self._due(20))
        empty_db.commit()
        row = empty_db.execute("SELECT status,overdue_days FROM b2b_invoices WHERE invoice_id=?", (inv_id,)).fetchone()
        assert row["status"] == "overdue"
        assert row["overdue_days"] >= 19

    def test_send_reminder(self, empty_db):
        import b2b_recovery as b2b
        mid = _merchant(empty_db)
        inv_id, _ = b2b.create_invoice(empty_db, mid, "Corp B", amount=5000, due_at=self._due(5))
        empty_db.commit()
        r = b2b.send_reminder(empty_db, mid, inv_id)
        empty_db.commit()
        assert r["ok"] is True

    def test_mark_paid(self, empty_db):
        import b2b_recovery as b2b
        mid = _merchant(empty_db)
        inv_id, _ = b2b.create_invoice(empty_db, mid, "PaidCo", amount=20000, due_at=self._due(3))
        empty_db.commit()
        r = b2b.mark_paid(empty_db, mid, inv_id, paid_amount=20000)
        empty_db.commit()
        assert r["ok"] is True
        row = empty_db.execute("SELECT status FROM b2b_invoices WHERE invoice_id=?", (inv_id,)).fetchone()
        assert row["status"] == "paid"

    def test_aging_summary(self, empty_db):
        import b2b_recovery as b2b
        mid = _merchant(empty_db)
        b2b.create_invoice(empty_db, mid, "A", amount=1000, due_at=self._due(10))
        b2b.create_invoice(empty_db, mid, "B", amount=2000, due_at=self._due(40))
        empty_db.commit()
        a = b2b.aging_summary(empty_db, mid)
        assert a["data_type"] == "ACTUAL"
        assert a["total_outstanding"] >= 3000

    def test_merchant_isolation_b2b(self, empty_db):
        import b2b_recovery as b2b
        m1 = _merchant(empty_db, "B2B-M1"); m2 = _merchant(empty_db, "B2B-M2")
        b2b.create_invoice(empty_db, m1, "Corp", amount=5000, due_at=self._due(5))
        empty_db.commit()
        assert len(b2b.get_invoices(empty_db, m2)) == 0


class TestPromiseTracker:
    def _future(self, d=3):
        return (datetime.now(timezone.utc)+timedelta(days=d)).isoformat()
    def _past(self, d=2):
        return (datetime.now(timezone.utc)-timedelta(days=d)).isoformat()

    def test_create_promise(self, empty_db):
        import promise_tracker as pt
        mid = _merchant(empty_db)
        pid = pt.create_promise(empty_db, mid, 5000, self._future(5),
                                customer_name="Ravi", is_demo=1)
        empty_db.commit()
        row = empty_db.execute("SELECT * FROM promises WHERE promise_id=?", (pid,)).fetchone()
        assert row["merchant_id"] == mid

    def test_past_promise_is_missed_on_create(self, empty_db):
        import promise_tracker as pt
        mid = _merchant(empty_db)
        pid = pt.create_promise(empty_db, mid, 3000, self._past(2), is_demo=1)
        empty_db.commit()
        row = empty_db.execute("SELECT status FROM promises WHERE promise_id=?", (pid,)).fetchone()
        assert row["status"] == "missed"

    def test_mark_paid(self, empty_db):
        import promise_tracker as pt
        mid = _merchant(empty_db)
        pid = pt.create_promise(empty_db, mid, 8000, self._future(3), is_demo=1)
        empty_db.commit()
        r = pt.mark_paid(empty_db, mid, pid, actual_amount=8000)
        empty_db.commit()
        assert r["ok"] is True
        assert r["paid_amount"] == 8000

    def test_summary_metrics(self, empty_db):
        import promise_tracker as pt
        mid = _merchant(empty_db)
        pt.create_promise(empty_db, mid, 1000, self._future(2), is_demo=1)
        pt.create_promise(empty_db, mid, 2000, self._future(4), is_demo=1)
        empty_db.commit()
        s = pt.summary(empty_db, mid, is_demo=1)
        assert s["total_promises"] == 2
        assert s["data_type"] == "ACTUAL"

    def test_merchant_isolation_promises(self, empty_db):
        import promise_tracker as pt
        m1 = _merchant(empty_db, "PR-M1"); m2 = _merchant(empty_db, "PR-M2")
        pt.create_promise(empty_db, m1, 5000, self._future(1), is_demo=1)
        empty_db.commit()
        assert len(pt.get_promises(empty_db, m2)) == 0


class TestMultilingual:
    def test_english_message(self):
        import multilingual as ml
        case = {"scenario_type":"failed_payment","amount":5000,"failure_reason":"insufficient_funds"}
        r = ml.generate_recovery_message(case, language="en")
        assert "5,000" in r["message"] or "5000" in r["message"]
        assert r["language"] == "en"

    def test_hinglish_message(self):
        import multilingual as ml
        case = {"scenario_type":"mandate_retry","amount":3000,"failure_reason":"insufficient_funds"}
        r = ml.generate_recovery_message(case, language="hinglish")
        assert r["language"] == "hinglish"
        assert r["message"]

    def test_all_languages_returned(self):
        import multilingual as ml
        case = {"scenario_type":"checkout_abandonment","amount":1000}
        r = ml.generate_all_languages(case)
        for lang in ["en","hinglish","hi"]:
            assert lang in r

    def test_message_always_generated_for_any_language(self):
        import multilingual as ml
        case = {"scenario_type":"failed_payment","amount":1000}
        r = ml.generate_recovery_message(case, language="zz")
        assert r["message"]

    def test_voice_script_generated(self):
        import multilingual as ml
        case = {"scenario_type":"failed_payment","amount":10000}
        r = ml.generate_voice_script(case, language="en", call_intent="recovery_reminder")
        assert r["script_text"]
        assert r["status"] == "READY_FOR_PROVIDER"
        assert r["execution_mode"] == "SIMULATED"
        assert r["data_type"] == "SIMULATED"

    def test_data_type_always_simulated(self):
        import multilingual as ml
        case = {"scenario_type":"b2b_receivable","amount":50000}
        r = ml.generate_recovery_message(case, language="en")
        assert r["data_type"] == "SIMULATED"


class TestChannelEngine:
    def _policy(self):
        return {"preferred_channel":"email","voice_recovery_enabled":0,
                "working_hours_start":9,"working_hours_end":20}

    def test_select_channel_returns_valid(self):
        import channel_engine as ce
        case = {"scenario_type":"failed_payment","amount":5000,"priority":"medium"}
        r = ce.select_channel(case, self._policy())
        assert r["selected_channel"] in ce.ALL_CHANNELS
        assert r["rationale"]
        assert r["data_type"] == "ESTIMATED"

    def test_channel_ev_positive_for_high_amount(self):
        import channel_engine as ce
        ev = ce._channel_ev("email", 0.5, 50000)
        assert ev > 0

    def test_voice_script_status(self, empty_db):
        import channel_engine as ce, recovery_orchestrator as orch
        mid = _merchant(empty_db)
        cid = orch.create_case(empty_db, mid, "failed_payment", amount=5000)
        empty_db.commit()
        r = ce.create_voice_script(empty_db, cid, mid, language="en")
        assert r["ok"] is True
        assert r["status"] == "READY_FOR_PROVIDER"
        assert r["execution_mode"] == "SIMULATED"


class TestMandateSequencer:
    def _policy(self):
        return {"max_retries":3,"retry_cooldown_hours":24,"min_expected_value_rs":0}

    def test_retry_decision_insufficient_funds(self):
        import mandate_sequencer as ms
        case = {"scenario_type":"mandate_retry","amount":5000,"failure_reason":"insufficient_funds"}
        d = ms.compute_retry_decision(case, 1, self._policy())
        assert d["should_retry"] is True
        assert d["data_type"] == "ESTIMATED"

    def test_mandate_revoked_never_retries(self):
        import mandate_sequencer as ms
        case = {"scenario_type":"mandate_retry","amount":5000,"failure_reason":"mandate_revoked"}
        d = ms.compute_retry_decision(case, 1, self._policy())
        assert d["should_retry"] is False
        assert "revoked" in d["reason"].lower()

    def test_max_retries_stops(self):
        import mandate_sequencer as ms
        case = {"scenario_type":"mandate_retry","amount":1000,"failure_reason":"insufficient_funds"}
        d = ms.compute_retry_decision(case, 3, self._policy())
        assert d["should_retry"] is False

    def test_bank_technical_fast_retry(self):
        import mandate_sequencer as ms
        case = {"scenario_type":"mandate_retry","amount":5000,"failure_reason":"bank_technical_error"}
        d = ms.compute_retry_decision(case, 1, self._policy())
        assert d["should_retry"] is True
        assert d["signals"].get("fast_retry") is True

    def test_mandate_expired_blocked(self):
        import mandate_sequencer as ms
        case = {"scenario_type":"mandate_retry","amount":5000,"failure_reason":"mandate_expired"}
        d = ms.compute_retry_decision(case, 1, self._policy())
        assert d["should_retry"] is False


class TestPolicyCenter:
    def test_defaults_when_no_policy(self, empty_db):
        import policy_center as pc
        mid = _merchant(empty_db)
        pol = pc.get_merchant_policy(empty_db, mid)
        assert pol["max_retries"] == 3
        assert pol["preferred_channel"] == "email"

    def test_create_policy(self, empty_db):
        import policy_center as pc
        mid = _merchant(empty_db)
        r = pc.upsert_policy(empty_db, mid, max_retries=5, preferred_channel="sms")
        assert r["ok"] is True
        assert r["policy"]["max_retries"] == 5

    def test_invalid_channel_rejected(self, empty_db):
        import policy_center as pc
        mid = _merchant(empty_db)
        r = pc.upsert_policy(empty_db, mid, preferred_channel="carrier_pigeon")
        assert r["ok"] is False

    def test_invalid_language_rejected(self, empty_db):
        import policy_center as pc
        mid = _merchant(empty_db)
        r = pc.upsert_policy(empty_db, mid, preferred_language="klingon")
        assert r["ok"] is False

    def test_retries_out_of_bounds(self, empty_db):
        import policy_center as pc
        mid = _merchant(empty_db)
        r = pc.upsert_policy(empty_db, mid, max_retries=99)
        assert r["ok"] is False

    def test_reset_to_defaults(self, empty_db):
        import policy_center as pc
        mid = _merchant(empty_db)
        pc.upsert_policy(empty_db, mid, max_retries=7)
        pc.reset_to_defaults(empty_db, mid)
        pol = pc.get_merchant_policy(empty_db, mid)
        assert pol["max_retries"] == 3


class TestDemoEngine:
    def test_demo_runs_full_flow(self, empty_db):
        import demo_engine as de
        de.get_or_create_demo_merchant(empty_db)
        r = de.run_full_demo(empty_db)
        empty_db.commit()
        assert r["ok"] is True
        assert len(r["demo_steps"]) == 10
        assert r["data_type"] == "SIMULATED"
        assert "isolated" in r["isolation_note"].lower()

    def test_demo_isolated_from_real_data(self, empty_db):
        import demo_engine as de, recovery_orchestrator as orch
        real_mid = _merchant(empty_db, "REAL-M")
        real_cid = orch.create_case(empty_db, real_mid, "failed_payment", amount=9999)
        empty_db.commit()
        de.get_or_create_demo_merchant(empty_db)
        de.run_full_demo(empty_db)
        empty_db.commit()
        real_cases = orch.get_cases(empty_db, real_mid)
        assert len(real_cases) == 1
        assert real_cases[0]["case_id"] == real_cid

    def test_demo_reset_clears_only_demo(self, empty_db):
        import demo_engine as de, recovery_orchestrator as orch
        real_mid = _merchant(empty_db, "REAL-M2")
        orch.create_case(empty_db, real_mid, "failed_payment", amount=1000, is_demo=0)
        empty_db.commit()
        de.get_or_create_demo_merchant(empty_db)
        de.run_full_demo(empty_db)
        de.reset_demo(empty_db)
        empty_db.commit()
        real_cases = orch.get_cases(empty_db, real_mid, is_demo=0)
        assert len(real_cases) == 1


class TestDegradationInvestigator:
    def test_investigate_empty(self, empty_db):
        import degradation_investigator as di
        mid = _merchant(empty_db)
        r = di.investigate(empty_db, mid)
        assert r["ok"] is False

    def test_investigate_with_data(self, fresh_db):
        import degradation_investigator as di
        mid = _merchant(fresh_db)
        r = di.investigate(fresh_db, mid)
        assert r["ok"] is True
        assert "revenue_at_risk_rs" in r
        assert r["data_type"] == "actual"

    def test_causal_note_present(self, fresh_db):
        import degradation_investigator as di
        mid = _merchant(fresh_db)
        r = di.investigate(fresh_db, mid)
        assert "causal" in r.get("causal_language_note","").lower()

    def test_record_degradation_event(self, empty_db):
        import degradation_investigator as di
        mid = _merchant(empty_db)
        eid = di.record_degradation_event(
            empty_db, mid, "bank_degradation", "bank=HDFC",
            "HDFC UPI dropped 30%", severity="warning", revenue_at_risk=150000)
        empty_db.commit()
        events = di.get_degradation_events(empty_db, mid)
        assert len(events) == 1
        assert events[0]["severity"] == "warning"


class TestMerchantIsolation:
    def test_portfolio_isolated(self, empty_db):
        import recovery_orchestrator as o
        m1 = _merchant(empty_db,"ISO-M1"); m2 = _merchant(empty_db,"ISO-M2")
        for _ in range(3):
            o.create_case(empty_db, m1, "failed_payment", amount=1000)
        empty_db.commit()
        p = o.measure_portfolio(empty_db, m2)
        assert p["total_cases"] == 0

    def test_queue_isolated(self, empty_db):
        import recovery_orchestrator as o
        m1 = _merchant(empty_db,"PQ-M1"); m2 = _merchant(empty_db,"PQ-M2")
        o.create_case(empty_db, m1, "failed_payment", amount=50000, priority="critical")
        empty_db.commit()
        assert len(o.priority_queue(empty_db, m2)) == 0

    def test_timeline_isolated(self, empty_db):
        import recovery_orchestrator as o
        m1 = _merchant(empty_db,"TL-M1"); m2 = _merchant(empty_db,"TL-M2")
        cid = o.create_case(empty_db, m1, "failed_payment", amount=1000)
        empty_db.commit()
        assert len(o.get_timeline(empty_db, cid, m2)) == 0

    def test_demo_flag_separation(self, empty_db):
        import recovery_orchestrator as o
        mid = _merchant(empty_db)
        o.create_case(empty_db, mid, "failed_payment", amount=1000, is_demo=0)
        o.create_case(empty_db, mid, "failed_payment", amount=2000, is_demo=1)
        empty_db.commit()
        real = o.get_cases(empty_db, mid, is_demo=0)
        demo = o.get_cases(empty_db, mid, is_demo=1)
        assert len(real) == 1 and len(demo) == 1
        assert real[0]["amount"] == 1000
        assert demo[0]["amount"] == 2000
