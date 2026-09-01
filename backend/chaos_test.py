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
    overall = "ALL 7 ATTACKS DEFENDED" if report["passed"] else (
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
