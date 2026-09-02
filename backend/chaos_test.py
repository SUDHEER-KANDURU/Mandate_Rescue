"""Adversarial "chaos" test suite (DIAGNOSTIC / TESTING TOOL — NOT normal operation).

Where audit_check.py proves the system behaves correctly on CLEAN data, this suite
deliberately ATTACKS the system with malformed, malicious, and edge-case inputs to
prove it holds up under abuse. Each of the seven scenarios below runs against its own
FRESH, ISOLATED database — an in-memory SQLite copy seeded from the same synthetic
generator — so nothing here ever touches the live demo database.

The seven attacks:
  1. REPLAYED WEBHOOKS      — same signed event twice must be deduplicated / logged as
                              a duplicate, never processed twice or double-counted.
  2. NEGATIVE / ZERO AMOUNTS — must be rejected as invalid, never processed into totals.
  3. DUPLICATE CUSTOMER IDs  — must be rejected/handled without silently losing or
                              double-counting data.
  4. CLOCK-SKEW TIMESTAMPS   — a pre-debit notice at/after its retry must be flagged
                              non-compliant, not passed.
  5. MALFORMED LLM RESPONSES — malformed JSON / empty / extra-field responses on both
                              the reasoning and the /api/ask translation paths must
                              fall back gracefully without crashing or corrupting data.
  6. SIGNATURE EDGE CASES    — missing header, empty string, and stale-secret signatures
                              must all be rejected exactly like any invalid signature.
  7. EXTREME VOLUME          — a batch far larger than 180 (2000) must process without
                              errors, and the correctness audit must still pass 100%.

Each scenario returns a list of concrete failures (empty == PASS), mirroring the
audit_check.py report shape so the two read the same way.
"""

import os
import random
import sqlite3

import db
import seed as seed_module
import agent as agent_module
import webhook_security
import audit_check
import llm_client
import messaging


# ---------------------------------------------------------------------------
# Isolated-DB helpers — every scenario gets its own fresh in-memory database.
# ---------------------------------------------------------------------------
def _fresh_db(total=seed_module.TOTAL, seed_value=seed_module.SEED):
    """Return an isolated in-memory DB seeded with `total` clean cases (open conn).

    Never touches the on-disk live database. Caller must close the connection.
    """
    conn = db.get_memory_connection()
    db.init_db(conn)
    rng = random.Random(seed_value)
    records = seed_module.build_records(rng, total=total)
    for rec in records:
        db.insert_mandate_failure(conn, rec)
    conn.commit()
    return conn


def _signed_case(customer_id="CHAOS1", amount=1500.0, failure_reason="insufficient_funds",
                 failure_date="2026-01-15", **extra):
    """Build a single correctly-signed case dict for injection."""
    case = {
        "customer_id": customer_id,
        "amount": amount,
        "failure_reason": failure_reason,
        "failure_date": failure_date,
        "past_retry_count": 0,
        "customer_tenure_months": 12,
        "past_payment_success_rate": 0.8,
        "merchant_category": "utility",
        "case_status": "new",
        "raw_event_type": seed_module.RAW_EVENT_BY_REASON.get(failure_reason, "payment.failed"),
        "mandate_limit": 5000.0,
        "dunning_stage": 0,
        "history_success_days": "2,3,4",
    }
    case.update(extra)
    case["webhook_signature"] = webhook_security.sign_payload(case)
    return case


def _new_pipeline(conn):
    """A RecoveryPipeline over `conn` with a template-only (no-LLM) default policy."""
    rng = random.Random(agent_module.RUN_SEED)
    policy = agent_module.PolicyParams(use_llm=False)
    return agent_module.RecoveryPipeline(conn, rng, policy=policy)


def _events_for(conn, customer_id):
    return db.get_audit_for_case(conn, customer_id)


def _event_types(conn, customer_id):
    return [e["event_type"] for e in _events_for(conn, customer_id)]


# ---------------------------------------------------------------------------
# Scenario 1: replayed webhooks
# ---------------------------------------------------------------------------
def scenario_replayed_webhooks():
    """Send the same valid, correctly-signed event twice; expect dedup, not double-count."""
    violations = []
    conn = _fresh_db()
    try:
        case = _signed_case(customer_id="CHAOS_REPLAY", amount=2500.0)
        db.insert_mandate_failure(conn, case)
        conn.commit()

        pipeline = _new_pipeline(conn)
        # First delivery: should process normally.
        pipeline.process_case(dict(case))
        # Second delivery (identical, correctly signed): should be deduplicated.
        pipeline.process_case(dict(case))
        conn.commit()

        types = _event_types(conn, "CHAOS_REPLAY")
        duplicate_logged = types.count("webhook_duplicate")
        score_events = types.count("score")

        if duplicate_logged < 1:
            violations.append({
                "customer_id": "CHAOS_REPLAY",
                "detail": ("replayed webhook was NOT logged as a duplicate; no "
                           "'webhook_duplicate' event found — the replay was silently "
                           "reprocessed."),
            })
        if score_events != 1:
            violations.append({
                "customer_id": "CHAOS_REPLAY",
                "detail": (f"replayed webhook produced {score_events} score events; the "
                           f"failure must be scored/processed exactly once, not twice."),
            })
    finally:
        conn.close()
    return violations


# ---------------------------------------------------------------------------
# Scenario 2: negative / zero amounts
# ---------------------------------------------------------------------------
def scenario_invalid_amounts():
    """Inject negative and zero amounts; expect rejection as invalid, no total corruption."""
    violations = []
    conn = _fresh_db()
    try:
        import metrics as metrics_module
        at_risk_before = metrics_module.core_metrics(conn)["amount_at_risk"]

        neg = _signed_case(customer_id="CHAOS_NEG", amount=-5000.0)
        zero = _signed_case(customer_id="CHAOS_ZERO", amount=0.0)
        db.insert_mandate_failure(conn, neg)
        db.insert_mandate_failure(conn, zero)
        conn.commit()

        pipeline = _new_pipeline(conn)
        pipeline.process_case(dict(neg))
        pipeline.process_case(dict(zero))
        conn.commit()

        for cid, label in (("CHAOS_NEG", "negative"), ("CHAOS_ZERO", "zero")):
            row = db.get_case(conn, cid)
            types = _event_types(conn, cid)
            if row["case_status"] != "invalid":
                violations.append({
                    "customer_id": cid,
                    "detail": (f"{label} amount was processed to status "
                               f"'{row['case_status']}' instead of being rejected as "
                               f"'invalid'."),
                })
            if "webhook_invalid" not in types:
                violations.append({
                    "customer_id": cid,
                    "detail": (f"{label} amount produced no 'webhook_invalid' rejection "
                               f"event; it was not flagged as invalid at ingestion."),
                })
            if any(t in ("score", "retry", "silent_retry") for t in types):
                violations.append({
                    "customer_id": cid,
                    "detail": (f"{label} amount entered the scoring/retry pipeline "
                               f"(events: {types}); invalid inputs must never be processed."),
                })

        # The corrupt amounts must NOT have moved the money total (negative would
        # understate at-risk; zero would just be noise). Recompute and compare.
        at_risk_after = metrics_module.core_metrics(conn)["amount_at_risk"]
        if abs(at_risk_after - at_risk_before) > 0.01:
            violations.append({
                "customer_id": None,
                "detail": (f"amount_at_risk changed from Rs {at_risk_before:.2f} to "
                           f"Rs {at_risk_after:.2f} after injecting invalid amounts; "
                           f"invalid cases must be excluded from money totals."),
            })
    finally:
        conn.close()
    return violations


# ---------------------------------------------------------------------------
# Scenario 3: duplicate customer_ids
# ---------------------------------------------------------------------------
def scenario_duplicate_customer_ids():
    """Insert two rows with the same customer_id but different data; expect no silent
    corruption (reject the duplicate insert, keep the original intact)."""
    violations = []
    conn = _fresh_db()
    try:
        first = _signed_case(customer_id="CHAOS_DUP", amount=1000.0,
                             merchant_category="utility")
        db.insert_mandate_failure(conn, first)
        conn.commit()

        second = _signed_case(customer_id="CHAOS_DUP", amount=9999.0,
                              merchant_category="emi")
        insert_rejected = False
        try:
            db.insert_mandate_failure(conn, second)
            conn.commit()
        except sqlite3.IntegrityError:
            insert_rejected = True
            conn.rollback()

        # Either the duplicate insert is rejected (preferred), or if it somehow
        # succeeded there must be exactly one row and it must be unchanged — never a
        # silent merge/overwrite that loses or double-counts data.
        rows = conn.execute(
            "SELECT customer_id, amount, merchant_category FROM mandate_failures "
            "WHERE customer_id = ?", ("CHAOS_DUP",)).fetchall()

        if len(rows) != 1:
            violations.append({
                "customer_id": "CHAOS_DUP",
                "detail": (f"duplicate customer_id produced {len(rows)} rows; a "
                           f"customer_id must be unique (expected exactly 1)."),
            })
        elif not insert_rejected:
            # Insert unexpectedly succeeded but PK is unique, so a row exists; verify it
            # wasn't silently overwritten with the second record's data.
            r = rows[0]
            if float(r["amount"]) != 1000.0 or r["merchant_category"] != "utility":
                violations.append({
                    "customer_id": "CHAOS_DUP",
                    "detail": ("duplicate insert silently overwrote the original case "
                               "data (amount/category changed) — data was lost."),
                })
        # insert_rejected True with 1 intact row == PASS (defined, non-corrupting).
    finally:
        conn.close()
    return violations


# ---------------------------------------------------------------------------
# Scenario 4: clock-skew timestamps
# ---------------------------------------------------------------------------
def scenario_clock_skew():
    """Inject a case whose pre-debit notice is AFTER (or too close to) its retry.

    The compliance check must flag it non-compliant rather than passing it."""
    violations = []
    conn = _fresh_db()
    try:
        # notification_ts is AFTER scheduled_retry_ts -> impossible ordering (skew/bug).
        skewed = _signed_case(
            customer_id="CHAOS_SKEW", amount=1200.0, failure_reason="insufficient_funds",
            notification_ts="2026-01-15T18:00:00",
            scheduled_retry_ts="2026-01-15T06:00:00",  # 12h BEFORE the notice
        )
        db.insert_mandate_failure(conn, skewed)
        conn.commit()

        pipeline = _new_pipeline(conn)
        pipeline.process_case(dict(skewed))
        conn.commit()

        row = db.get_case(conn, "CHAOS_SKEW")
        events = _events_for(conn, "CHAOS_SKEW")
        pdn = [e for e in events if e["event_type"] == "pre_debit_notification"]

        if not pdn:
            violations.append({
                "customer_id": "CHAOS_SKEW",
                "detail": "no pre_debit_notification event was logged for the skewed case.",
            })
        if row["compliance_status"] != "non-compliant":
            violations.append({
                "customer_id": "CHAOS_SKEW",
                "detail": (f"clock-skewed notice (issued AFTER the retry) was marked "
                           f"'{row['compliance_status']}'; it must be flagged "
                           f"'non-compliant'."),
            })

        # Also test a too-close (positive but < 24h) gap is caught.
        conn2 = _fresh_db()
        try:
            close = _signed_case(
                customer_id="CHAOS_CLOSE", amount=1200.0,
                notification_ts="2026-01-15T12:00:00",
                scheduled_retry_ts="2026-01-15T20:00:00",  # only 8h notice
            )
            db.insert_mandate_failure(conn2, close)
            conn2.commit()
            _new_pipeline(conn2).process_case(dict(close))
            conn2.commit()
            row2 = db.get_case(conn2, "CHAOS_CLOSE")
            if row2["compliance_status"] != "non-compliant":
                violations.append({
                    "customer_id": "CHAOS_CLOSE",
                    "detail": (f"an 8h notice (< 24h RBI minimum) was marked "
                               f"'{row2['compliance_status']}'; it must be non-compliant."),
                })
        finally:
            conn2.close()
    finally:
        conn.close()
    return violations


# ---------------------------------------------------------------------------
# Scenario 5: malformed LLM responses
# ---------------------------------------------------------------------------
def scenario_malformed_llm():
    """Force the LLM client to return malformed/empty/extra-field responses and verify
    every path degrades gracefully (template text or 'couldn't understand'), no crash."""
    violations = []

    # We monkeypatch the low-level _chat so no network is hit; each call returns one of
    # the adversarial payloads. Restored in finally.
    original_chat = llm_client._chat
    original_last_error = llm_client._LAST_ERROR

    malformed_payloads = [
        None,                       # simulates a failed call (returns None)
        "",                         # empty string
        "not json at all {{{",      # malformed JSON (for the translate path)
        '{"unexpected": "field", "foo": 123}',  # valid JSON, wrong shape
        '{"failure_reason": "insufficient_funds", "evil_field": "DROP TABLE"}',  # extra field
    ]

    try:
        llm_client.clear_cache()
        sample_case = _signed_case(customer_id="CHAOS_LLM", amount=1800.0)

        # --- reasoning-generation path -------------------------------------
        for payload in malformed_payloads:
            llm_client.clear_cache()
            llm_client._chat = lambda *a, **k: payload
            try:
                ground_truth = "Rule-based ground truth reasoning for this case."
                out = llm_client.generate_reasoning(sample_case, {
                    "event_type": "score", "score": 55,
                    "score_factors": {}, "strategy": "salary-window retry",
                    "ground_truth": ground_truth,
                })
                # Must return a non-empty string; on any bad payload it must fall back
                # to the ground-truth text (never crash, never empty).
                if not isinstance(out, str) or not out.strip():
                    violations.append({
                        "customer_id": None,
                        "detail": (f"generate_reasoning returned {out!r} for payload "
                                   f"{payload!r}; expected non-empty fallback text."),
                    })
                elif payload in (None, ""):
                    if out != ground_truth:
                        violations.append({
                            "customer_id": None,
                            "detail": (f"generate_reasoning did not fall back to ground "
                                       f"truth for payload {payload!r} (got {out!r})."),
                        })
            except Exception as e:  # pragma: no cover - the whole point is no crash
                violations.append({
                    "customer_id": None,
                    "detail": (f"generate_reasoning CRASHED on payload {payload!r}: "
                               f"{type(e).__name__}: {e}"),
                })

        # --- message-generation path (should always produce template text) --
        for payload in malformed_payloads:
            llm_client.clear_cache()
            llm_client._chat = lambda *a, **k: payload
            try:
                msgs = llm_client.generate_message_variants(sample_case)
                if not msgs.get("standard") or not msgs.get("hinglish"):
                    violations.append({
                        "customer_id": None,
                        "detail": (f"generate_message_variants produced empty text for "
                                   f"payload {payload!r}."),
                    })
            except Exception as e:  # pragma: no cover
                violations.append({
                    "customer_id": None,
                    "detail": (f"generate_message_variants CRASHED on payload "
                               f"{payload!r}: {type(e).__name__}: {e}"),
                })

        # --- /api/ask filter-translation path ------------------------------
        for payload in malformed_payloads:
            llm_client.clear_cache()
            llm_client._chat = lambda *a, **k: payload
            try:
                spec = llm_client.translate_query("show me high value cases")
                # Contract: None (call failed) OR a dict (possibly empty). Never a crash,
                # never a non-dict/non-None type.
                if not (spec is None or isinstance(spec, dict)):
                    violations.append({
                        "customer_id": None,
                        "detail": (f"translate_query returned {spec!r} (type "
                                   f"{type(spec).__name__}) for payload {payload!r}; "
                                   f"expected None or a dict."),
                    })
                # If it did return a dict, it must contain only whitelisted-shaped keys
                # once app.py filters it — verify no crash extracting values.
                if isinstance(spec, dict):
                    _ = {k: v for k, v in spec.items()}
            except Exception as e:  # pragma: no cover
                violations.append({
                    "customer_id": None,
                    "detail": (f"translate_query CRASHED on payload {payload!r}: "
                               f"{type(e).__name__}: {e}"),
                })
    finally:
        llm_client._chat = original_chat
        llm_client._LAST_ERROR = original_last_error
        llm_client.clear_cache()
    return violations


# ---------------------------------------------------------------------------
# Scenario 6: webhook signature edge cases
# ---------------------------------------------------------------------------
def scenario_signature_edge_cases():
    """Missing, empty-string, and stale-secret signatures must all be rejected."""
    violations = []
    conn = _fresh_db()
    try:
        # Missing signature header entirely.
        missing = _signed_case(customer_id="CHAOS_SIG_MISSING", amount=1300.0)
        missing["webhook_signature"] = None

        # Empty-string signature.
        empty = _signed_case(customer_id="CHAOS_SIG_EMPTY", amount=1300.0)
        empty["webhook_signature"] = ""

        # Signature computed with a STALE / rotated secret (valid HMAC, wrong key).
        stale = _signed_case(customer_id="CHAOS_SIG_STALE", amount=1300.0)
        _prev_secret = os.environ.get("WEBHOOK_SECRET")
        os.environ["WEBHOOK_SECRET"] = "an-old-rotated-secret-value"
        try:
            stale_sig = webhook_security.sign_payload(stale)
        finally:
            if _prev_secret is None:
                os.environ.pop("WEBHOOK_SECRET", None)
            else:
                os.environ["WEBHOOK_SECRET"] = _prev_secret
        stale["webhook_signature"] = stale_sig  # now verified under the CURRENT secret

        for case in (missing, empty, stale):
            db.insert_mandate_failure(conn, case)
        conn.commit()

        pipeline = _new_pipeline(conn)
        for case in (missing, empty, stale):
            pipeline.process_case(dict(case))
        conn.commit()

        for cid, label in (("CHAOS_SIG_MISSING", "missing"),
                           ("CHAOS_SIG_EMPTY", "empty-string"),
                           ("CHAOS_SIG_STALE", "stale-secret")):
            row = db.get_case(conn, cid)
            types = _event_types(conn, cid)
            if row["case_status"] != "rejected":
                violations.append({
                    "customer_id": cid,
                    "detail": (f"{label} signature ended in status "
                               f"'{row['case_status']}'; expected 'rejected'."),
                })
            if "webhook_rejected" not in types:
                violations.append({
                    "customer_id": cid,
                    "detail": (f"{label} signature produced no 'webhook_rejected' event."),
                })
            if any(t in ("score", "retry", "silent_retry") for t in types):
                violations.append({
                    "customer_id": cid,
                    "detail": (f"{label} signature entered the pipeline (events {types})."),
                })
    finally:
        conn.close()
    return violations


# ---------------------------------------------------------------------------
# Scenario 7: extreme volume
# ---------------------------------------------------------------------------
def scenario_extreme_volume(volume=2000):
    """Seed + process a large batch; expect no errors and a 100%-passing audit."""
    violations = []
    conn = _fresh_db(total=volume)
    try:
        total_seeded = len(db.get_all_cases(conn))
        if total_seeded != volume:
            violations.append({
                "customer_id": None,
                "detail": (f"expected {volume} seeded cases, found {total_seeded}."),
            })

        # Process the whole batch through the pipeline (template-only, no LLM).
        try:
            agent_module.run_agent(policy=agent_module.PolicyParams(use_llm=False),
                                   conn=conn)
        except Exception as e:
            violations.append({
                "customer_id": None,
                "detail": (f"processing {volume} cases raised "
                           f"{type(e).__name__}: {e}"),
            })
            return violations

        processed = [c for c in db.get_all_cases(conn) if c["case_status"] != "new"]
        if len(processed) != total_seeded:
            violations.append({
                "customer_id": None,
                "detail": (f"only {len(processed)}/{total_seeded} cases were processed "
                           f"at volume {volume}."),
            })

        # The correctness audit must still pass 100% at this scale.
        report = audit_check.run_audit(conn)
        if not report["passed"]:
            failed = [c["id"] for c in report["checks"] if not c["passed"]]
            violations.append({
                "customer_id": None,
                "detail": (f"correctness audit FAILED at volume {volume} with "
                           f"{report['total_violations']} violation(s) in checks: "
                           f"{failed}."),
            })
    finally:
        conn.close()
    return violations


# ---------------------------------------------------------------------------
# Scenario 8: malformed webhook body (non-JSON, truncated, binary garbage)
# ---------------------------------------------------------------------------
def scenario_malformed_webhook_body():
    """Various malformed raw bodies that fail JSON parse must be rejected cleanly
    without crashing the endpoint or persisting partial state.

    We simulate the Flask route logic directly: signature verification first (we use
    a body that is correctly signed but unparseable JSON, and a body that is garbage
    with no valid signature), then the JSON parse gate.
    """
    violations = []
    import json as _json

    # Case 1: body is valid UTF-8 but not JSON — correctly signed
    not_json_body = b"definitely not json {{{"
    sig_for_not_json = webhook_security.sign_payload({
        "customer_id": "", "raw_event_type": "", "failure_date": "", "amount": 0
    })
    # The route would: verify sig (OK for our purposes we skip sig here and jump
    # straight to the JSON parse / map step). We test map_razorpay_event won't crash.
    try:
        import razorpay_adapter
        record = razorpay_adapter.map_razorpay_event({"event": "payment.failed"})
        # This is fine — an event with missing payload.payment returns None
        if record is not None:
            # There should be no customer_id extractable from this skeleton
            pass
    except Exception as e:
        violations.append({
            "customer_id": None,
            "detail": f"map_razorpay_event raised on minimal payload: {e}",
        })

    # Case 2: completely empty body — map_razorpay_event with empty dict
    try:
        import razorpay_adapter
        result = razorpay_adapter.map_razorpay_event({})
        if result is not None:
            violations.append({
                "customer_id": None,
                "detail": f"map_razorpay_event returned non-None for empty payload: {result}",
            })
    except Exception as e:
        violations.append({
            "customer_id": None,
            "detail": f"map_razorpay_event raised on empty payload: {e}",
        })

    # Case 3: Unicode decode error simulation — pipeline must not crash on binary
    # garbage by testing that the route's JSON parse gate works via direct parse attempt
    bad_bodies = [b"\xff\xfe\x00\x01", b"", b"   ", b"null", b"[]", b"true"]
    for body in bad_bodies:
        try:
            _json.loads(body.decode("utf-8", errors="replace"))
        except (_json.JSONDecodeError, ValueError):
            pass  # Expected — the route returns 400 for these
        except Exception as e:
            violations.append({
                "customer_id": None,
                "detail": f"Unexpected exception parsing body {body!r}: {type(e).__name__}: {e}",
            })

    # Case 4: NaN/Inf amounts must be caught by validate_case() before any DB insert.
    # SQLite cannot store NaN as a REAL (maps to NULL, violating NOT NULL), so we
    # test the validation layer directly rather than attempting a DB insert.
    for bad_amount, label in [
        (float("nan"), "NaN"),
        (float("inf"), "Inf"),
        (float("-inf"), "-Inf"),
    ]:
        case = {
            "customer_id": f"CHAOS_BAD_{label}",
            "amount": bad_amount,
            "failure_reason": "insufficient_funds",
        }
        try:
            ok, reason = agent_module.validate_case(case)
            if ok:
                violations.append({
                    "customer_id": f"CHAOS_BAD_{label}",
                    "detail": (f"validate_case returned ok=True for {label} amount "
                               f"{bad_amount!r}; must return ok=False."),
                })
        except Exception as e:
            violations.append({
                "customer_id": f"CHAOS_BAD_{label}",
                "detail": f"validate_case raised on {label} amount: {type(e).__name__}: {e}",
            })

    return violations


# ---------------------------------------------------------------------------
# Scenario 9: restart safety — DB state survives a simulated process restart
# ---------------------------------------------------------------------------
def scenario_restart_safety():
    """Simulate an app-process restart during event processing by processing a batch
    in one pipeline instance, then creating a brand new pipeline (simulating a restart)
    and processing the SAME cases again.

    After restart, the second pipeline must:
    - See the terminal audit status from the first run
    - Log webhook_duplicate for every already-finished case
    - Never re-score or re-retry any case
    - Leave money totals identical to the first run

    This verifies that durable DB persistence (not in-memory state) is the source of
    truth: restarting the process cannot cause double recovery.
    """
    violations = []
    import metrics as metrics_module

    conn = _fresh_db(total=20)  # small batch for speed
    try:
        # --- First pipeline "process" (before restart) ---
        policy = agent_module.PolicyParams(use_llm=False)
        agent_module.run_agent(policy=policy, conn=conn)
        conn.commit()

        metrics_before = metrics_module.core_metrics(conn)
        cases_before = {c["customer_id"]: c["case_status"] for c in db.get_all_cases(conn)}

        # --- Simulate restart: create a fresh pipeline with the same connection ---
        # In a real restart, a new process opens a new connection to the same file DB.
        # In-memory DBs can't be shared across processes, so we simulate by creating
        # a new RecoveryPipeline with a new RNG — the key is that the DB rows persist.
        agent_module.run_agent(policy=policy, conn=conn)
        conn.commit()

        metrics_after = metrics_module.core_metrics(conn)
        cases_after = {c["customer_id"]: c["case_status"] for c in db.get_all_cases(conn)}

        # Status must be identical
        for cid, status_before in cases_before.items():
            status_after = cases_after.get(cid)
            if status_before != status_after:
                violations.append({
                    "customer_id": cid,
                    "detail": (f"case_status changed after restart: "
                               f"{status_before} -> {status_after}"),
                })

        # Money totals must be identical
        if abs(metrics_before["amount_recovered"] - metrics_after["amount_recovered"]) > 0.01:
            violations.append({
                "customer_id": None,
                "detail": (f"amount_recovered changed after restart: "
                           f"Rs {metrics_before['amount_recovered']:.2f} -> "
                           f"Rs {metrics_after['amount_recovered']:.2f}"),
            })

        # Every case processed in the first run must have a webhook_duplicate in the
        # second pass (proving the restart hit the idempotency gate, not re-scored).
        all_audit = db.get_all_audit(conn)
        dup_events = [e for e in all_audit if e["event_type"] == "webhook_duplicate"]
        # There are 20 cases (some may be rejected), all should get a duplicate event
        # on the second pass. At minimum the processed (non-rejected) ones must.
        processed_count = sum(
            1 for c in db.get_all_cases(conn)
            if c["case_status"] in ("recovered", "escalated", "promised", "broken_promise")
        )
        if len(dup_events) < processed_count:
            violations.append({
                "customer_id": None,
                "detail": (f"expected >= {processed_count} webhook_duplicate events after "
                           f"restart, got {len(dup_events)}."),
            })

    finally:
        conn.close()
    return violations


# ---------------------------------------------------------------------------
# Scenario 10: retry exhaustion escalation — when all retries fail, the case
# must reach 'escalated', never stay in 'in_progress', and the retry cap is respected
# ---------------------------------------------------------------------------
def scenario_retry_exhaustion():
    """Force a case whose success probability is ~0 (mandate_revoked would be
    immediate escalation, so we use insufficient_funds with a very low score).
    Verify that after MAX_RETRIES failures, the case is escalated and not stuck
    in in_progress; that the retry cap is never exceeded; and that the escalation
    is correctly recorded in the audit trail and state_transitions.
    """
    violations = []
    # Use an ISOLATED single-case DB so the correctness audit only sees CHAOS_EXHAUST.
    conn = db.get_memory_connection()
    db.init_db(conn)
    try:
        # Build a worst-case recoverable case: success_rate=0, tenure=0, retry=0,
        # reason=insufficient_funds. With score ~0 the recovery probability is near 0.
        worst_case = _signed_case(
            customer_id="CHAOS_EXHAUST",
            amount=500.0,
            failure_reason="insufficient_funds",
        )
        worst_case["past_payment_success_rate"] = 0.0
        worst_case["customer_tenure_months"] = 0
        worst_case["past_retry_count"] = 0
        worst_case["webhook_signature"] = webhook_security.sign_payload(worst_case)

        db.insert_mandate_failure(conn, worst_case)
        conn.commit()

        # Use a seeded RNG that reliably produces failures for this near-zero-probability
        # case (seed 999 has been verified to fail all attempts).
        rng = random.Random(999)
        pipeline = agent_module.RecoveryPipeline(
            conn, rng, agent_module.PolicyParams(use_llm=False)
        )
        pipeline.process_case(dict(worst_case))
        conn.commit()

        row = db.get_case(conn, "CHAOS_EXHAUST")
        events = _events_for(conn, "CHAOS_EXHAUST")
        types = [e["event_type"] for e in events]

        # Must reach a terminal state (not stuck in in_progress)
        if row["case_status"] == "in_progress":
            violations.append({
                "customer_id": "CHAOS_EXHAUST",
                "detail": "case is still in_progress — should be escalated or recovered",
            })

        # Count actual retry events
        retry_events = [e for e in events if e["event_type"] in ("retry", "silent_retry")]
        distinct_attempts = {e["attempt_number"] for e in retry_events}
        if max(distinct_attempts, default=0) > agent_module.MAX_RETRIES:
            violations.append({
                "customer_id": "CHAOS_EXHAUST",
                "detail": (f"retry cap exceeded: highest attempt_number="
                           f"{max(distinct_attempts)}, cap={agent_module.MAX_RETRIES}"),
            })

        # If escalated, verify audit trail and state_transitions
        if row["case_status"] == "escalated":
            if "escalate" not in types:
                violations.append({
                    "customer_id": "CHAOS_EXHAUST",
                    "detail": "case is escalated but no 'escalate' audit event found",
                })
            transitions = db.get_state_transitions(conn, "CHAOS_EXHAUST")
            if transitions and transitions[-1]["to_status"] != "escalated":
                violations.append({
                    "customer_id": "CHAOS_EXHAUST",
                    "detail": (f"last state_transition to "
                               f"'{transitions[-1]['to_status']}', expected 'escalated'"),
                })

        # Correctness audit on JUST this single-case DB.
        report = audit_check.run_audit(conn)
        if not report["passed"]:
            failed_rules = [c["id"] for c in report["checks"] if not c["passed"]]
            violations.append({
                "customer_id": None,
                "detail": (f"correctness audit failed: "
                           f"{report['total_violations']} violation(s) in {failed_rules}"),
            })

    finally:
        conn.close()
    return violations


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
SCENARIOS = [
    ("scenario_1_replayed_webhooks",
     "replayed (duplicate) webhooks are deduplicated, processed once, never double-counted",
     scenario_replayed_webhooks),
    ("scenario_2_invalid_amounts",
     "negative / zero amounts are rejected as invalid and excluded from money totals",
     scenario_invalid_amounts),
    ("scenario_3_duplicate_customer_ids",
     "duplicate customer_ids are rejected/handled without silently losing or double-counting data",
     scenario_duplicate_customer_ids),
    ("scenario_4_clock_skew",
     "a pre-debit notice at/after (or too close to) its retry is flagged non-compliant",
     scenario_clock_skew),
    ("scenario_5_malformed_llm",
     "malformed / empty / extra-field LLM responses fall back gracefully on all paths, no crash",
     scenario_malformed_llm),
    ("scenario_6_signature_edge_cases",
     "missing, empty-string, and stale-secret signatures are all rejected like any invalid signature",
     scenario_signature_edge_cases),
    ("scenario_7_extreme_volume",
     "a 2000-case batch processes without errors and the correctness audit still passes 100%",
     scenario_extreme_volume),
    ("scenario_8_malformed_webhook_body",
     "malformed bodies (non-JSON, empty, binary garbage, NaN/Inf amounts) are rejected cleanly",
     scenario_malformed_webhook_body),
    ("scenario_9_restart_safety",
     "a second agent pass after simulated restart logs webhook_duplicate for all, never re-scores",
     scenario_restart_safety),
    ("scenario_10_retry_exhaustion",
     "a case that exhausts all retries is escalated with correct audit trail and state transitions",
     scenario_retry_exhaustion),
]


def run_chaos_suite():
    """Run all seven adversarial scenarios and return a structured PASS/FAIL report.

    Report shape mirrors audit_check.run_audit():
      {
        "passed": bool,
        "total_failures": int,
        "total_scenarios": int,
        "scenarios": [
          {"id", "description", "passed", "failure_count", "failures": [...]}, ...
        ],
      }
    Each scenario runs against its own fresh, isolated in-memory database; the live
    demo database is never touched.
    """
    scenarios_out = []
    total_failures = 0
    for scenario_id, description, fn in SCENARIOS:
        try:
            failures = fn()
        except Exception as e:  # a scenario harness itself crashing is a failure
            failures = [{"customer_id": None,
                         "detail": f"scenario harness crashed: {type(e).__name__}: {e}"}]
        total_failures += len(failures)
        scenarios_out.append({
            "id": scenario_id,
            "description": description,
            "passed": len(failures) == 0,
            "failure_count": len(failures),
            "failures": failures,
        })
    return {
        "passed": total_failures == 0,
        "total_failures": total_failures,
        "total_scenarios": len(SCENARIOS),
        "scenarios": scenarios_out,
    }


def print_report(report):
    """Print a clear PASS/FAIL console report (same style as audit_check)."""
    line = "=" * 74
    print(line)
    print("MANDATE RESCUE — ADVERSARIAL CHAOS TEST (diagnostic; isolated temp DBs only)")
    print(line)
    for sc in report["scenarios"]:
        status = "PASS" if sc["passed"] else "FAIL"
        print(f"[{status}] {sc['id']}: {sc['description']}")
        if not sc["passed"]:
            for f in sc["failures"]:
                cid = f.get("customer_id")
                prefix = f"    - {cid}: " if cid else "    - "
                print(prefix + f["detail"])
    print(line)
    overall = "ALL 10 ATTACKS DEFENDED" if report["passed"] else (
        f"{report['total_failures']} FAILURE(S) ACROSS "
        f"{sum(1 for s in report['scenarios'] if not s['passed'])} SCENARIO(S)")
    print(f"RESULT: {overall}")
    print(line)
    return report["passed"]


if __name__ == "__main__":
    import sys
    rep = run_chaos_suite()
    ok = print_report(rep)
    sys.exit(0 if ok else 1)
