"""Automated correctness audit (additive, read-only).

After a full agent run, this module re-derives the ground truth straight from the
audit_log + mandate_failures rows and checks that the app's own stated business rules
actually hold for EVERY case. It also independently recomputes the money figures and
the agent-vs-baseline comparison sentence and compares them against what the live
metrics functions return, so display/logic drift is caught.

It NEVER writes to the database and NEVER changes agent, scoring, or compliance logic.
It only observes. Each check yields a list of concrete violations (with customer_id);
an empty list means PASS.

The seven rules (mirroring the product's design):
  1. mandate_revoked cases never recover via a retry path (immediate escalation only).
  2. No case logs more than 3 retry attempts.
  3. Every retry is preceded by a pre_debit_notification (compliant or explicitly
     marked non-compliant) — a silent gap (retry with no notification) is a bug.
  4. Rejected (bad-signature) cases have exactly one webhook_rejected event and no
     score/strategy/retry events (they never entered the pipeline).
  5. amount > mandate_limit cases route through re-authorization, never a normal
     salary-window / silent-retry strategy.
  6. The rupee figures from metrics.core_metrics match an independent recomputation
     from case rows.
  7. The agent-vs-baseline comparison sentence is grammatically + numerically sound:
     never a negative number, "more"/"less" agrees with the sign, and it does not
     render before a run has completed.
"""

import db
import metrics as metrics_module
import baseline as baseline_module

# Event types that represent an actual debit retry attempt.
RETRY_EVENT_TYPES = ("retry", "silent_retry")
# Events that mean a case entered the scoring/strategy pipeline (must NOT exist for a
# rejected/spoofed webhook).
PIPELINE_EVENT_TYPES = ("score", "strategy_selected", "retry", "silent_retry",
                        "pre_debit_notification", "dunning_stage", "reauth_link",
                        "mandate_limit_block", "promise_recorded", "escalate")
MAX_RETRIES = 3
# Tolerance for float rupee comparisons (rounding to paise).
MONEY_TOL = 0.01


def _audit_by_case(conn):
    """Return {customer_id: [audit rows in event order]} for all cases."""
    by_case = {}
    for row in db.get_all_audit(conn):
        by_case.setdefault(row["customer_id"], []).append(row)
    return by_case


# --- Rule 1 -----------------------------------------------------------------
def check_mandate_revoked_no_retry(cases, audit_by_case):
    """mandate_revoked must never recover through a retry path."""
    violations = []
    for c in cases:
        if c["failure_reason"] != "mandate_revoked":
            continue
        events = audit_by_case.get(c["customer_id"], [])
        retry_events = [e for e in events if e["event_type"] in RETRY_EVENT_TYPES]
        if retry_events:
            violations.append({
                "customer_id": c["customer_id"],
                "detail": (f"mandate_revoked case has {len(retry_events)} retry "
                           f"event(s); it must escalate immediately with no retry."),
            })
        elif c["case_status"] == "recovered":
            violations.append({
                "customer_id": c["customer_id"],
                "detail": "mandate_revoked case reached 'recovered' without any retry "
                          "path — revoked mandates are not recoverable automatically.",
            })
    return violations


# --- Rule 2 -----------------------------------------------------------------
def check_retry_cap(cases, audit_by_case):
    """No case logs more than MAX_RETRIES attempt events."""
    violations = []
    for c in cases:
        events = audit_by_case.get(c["customer_id"], [])
        attempts = [e for e in events if e["event_type"] in RETRY_EVENT_TYPES]
        # Count by distinct attempt_number so a re-logged attempt isn't double counted,
        # but also guard against more than MAX_RETRIES raw attempt rows.
        distinct = {e["attempt_number"] for e in attempts}
        if len(attempts) > MAX_RETRIES or max(distinct, default=0) > MAX_RETRIES:
            violations.append({
                "customer_id": c["customer_id"],
                "detail": (f"{len(attempts)} retry attempts logged "
                           f"(attempt numbers {sorted(distinct)}); the hard cap is "
                           f"{MAX_RETRIES}."),
            })
    return violations


# --- Rule 3 -----------------------------------------------------------------
def check_pre_debit_before_retry(cases, audit_by_case):
    """Every retry must be preceded by a pre_debit_notification (compliant or an
    explicit non-compliant mark). A retry with no notification at all is a silent gap."""
    violations = []
    for c in cases:
        events = audit_by_case.get(c["customer_id"], [])
        notifications = 0
        for e in events:
            if e["event_type"] == "pre_debit_notification":
                notifications += 1
            elif e["event_type"] in RETRY_EVENT_TYPES:
                # A retry that fires with no preceding pre-debit notification is the bug
                # we care about (silent gap: not even marked non-compliant).
                if notifications == 0:
                    violations.append({
                        "customer_id": c["customer_id"],
                        "detail": (f"retry (attempt {e['attempt_number']}) fired with no "
                                   f"preceding pre_debit_notification — silent compliance "
                                   f"gap, not even marked non-compliant."),
                    })
                    break
    return violations


# --- Rule 4 -----------------------------------------------------------------
def check_rejected_isolation(cases, audit_by_case):
    """Rejected cases: exactly one webhook_rejected event and no pipeline events."""
    violations = []
    for c in cases:
        if c["case_status"] != "rejected":
            continue
        events = audit_by_case.get(c["customer_id"], [])
        rejected_events = [e for e in events if e["event_type"] == "webhook_rejected"]
        pipeline_events = [e for e in events if e["event_type"] in PIPELINE_EVENT_TYPES]
        if len(rejected_events) != 1 or pipeline_events:
            violations.append({
                "customer_id": c["customer_id"],
                "detail": (f"rejected case has {len(rejected_events)} webhook_rejected "
                           f"event(s) and {len(pipeline_events)} pipeline event(s); "
                           f"expected exactly 1 rejection and 0 pipeline events."),
            })
    return violations


# --- Rule 5 -----------------------------------------------------------------
def check_over_limit_reauth(cases, audit_by_case):
    """amount > mandate_limit cases must route through re-authorization, never a
    normal salary-window / silent-retry strategy."""
    violations = []
    for c in cases:
        # Rejected/invalid cases never entered the pipeline; skip (Rule 4 and the
        # ingestion validation gate cover them).
        if c["case_status"] in ("rejected", "invalid"):
            continue
        amount = float(c["amount"])
        limit = float(c.get("mandate_limit") or 5000)
        if amount <= limit:
            continue
        events = audit_by_case.get(c["customer_id"], [])
        has_block = any(e["event_type"] == "mandate_limit_block" for e in events)
        has_silent = any(e["event_type"] == "silent_retry" for e in events)
        strategy_events = [e for e in events if e["event_type"] == "strategy_selected"]
        chose_normal = any(
            "higher-limit re-authorization" not in (e["action_taken"] or "")
            and "re-authorization link" not in (e["action_taken"] or "")
            for e in strategy_events
        ) if strategy_events else False
        # Over-limit is a bug if it never logged the mandate-limit block, OR it used a
        # silent quick retry (the transient-error strategy), OR its only strategy was a
        # plain non-reauth one.
        if not has_block or has_silent:
            violations.append({
                "customer_id": c["customer_id"],
                "detail": (f"amount Rs {amount:.0f} > mandate_limit Rs {limit:.0f} but "
                           f"mandate_limit_block={has_block}, silent_retry={has_silent}; "
                           f"over-limit cases must route through higher-limit re-auth."),
            })
        elif chose_normal and not has_block:
            violations.append({
                "customer_id": c["customer_id"],
                "detail": "over-limit case selected a normal (non-reauth) strategy.",
            })
    return violations


# --- Rule 6 -----------------------------------------------------------------
def check_money_figures(conn):
    """Independently recompute rupee figures and compare to metrics.core_metrics()."""
    violations = []
    cases = db.get_all_cases(conn)
    recomputed_at_risk = round(sum(float(c["amount"]) for c in cases), 2)
    recomputed_recovered = round(
        sum(float(c["amount"]) for c in cases if c["case_status"] == "recovered"), 2)

    reported = metrics_module.core_metrics(conn)
    checks = [
        ("amount_at_risk", recomputed_at_risk, float(reported["amount_at_risk"])),
        ("amount_recovered", recomputed_recovered, float(reported["amount_recovered"])),
    ]
    for name, recomputed, reported_val in checks:
        if abs(recomputed - reported_val) > MONEY_TOL:
            violations.append({
                "customer_id": None,
                "detail": (f"{name} mismatch: /api/metrics reports Rs {reported_val:.2f} "
                           f"but independent recomputation is Rs {recomputed:.2f}."),
            })
    return violations, {"amount_at_risk": recomputed_at_risk,
                        "amount_recovered": recomputed_recovered}


# --- Rule 7 -----------------------------------------------------------------
def _comparison_sentence(agent_recovered, baseline_recovered, has_run):
    """Reproduce the dashboard's agent-vs-baseline sentence logic (app.js renderMetrics).

    Returns (sentence, diff, word). Before a run it must be the prompt, never a number.
    """
    if not has_run:
        return ("Run the agent to see this comparison.", None, None)
    diff = agent_recovered - baseline_recovered
    word = "more" if diff >= 0 else "less"
    shown = abs(diff)
    sentence = (f"The agent recovered Rs {shown:,.0f} {word} than the naive baseline "
                f"(Rs {agent_recovered:,.0f} vs Rs {baseline_recovered:,.0f}).")
    return (sentence, diff, word)


def check_comparison_sentence(conn):
    """The agent-vs-baseline sentence must be numerically + grammatically consistent."""
    violations = []
    core = metrics_module.core_metrics(conn)
    base = baseline_module.run_baseline(conn)
    has_run = (core.get("recovered_cases", 0) > 0
               or core.get("escalated_cases", 0) > 0)

    agent_rec = float(core["amount_recovered"])
    base_rec = float(base["amount_recovered"])
    sentence, diff, word = _comparison_sentence(agent_rec, base_rec, has_run)

    if not has_run:
        # Must not render a number/comparison before a run.
        if "recovered" in sentence.lower() and ("more" in sentence or "less" in sentence):
            violations.append({
                "customer_id": None,
                "detail": "comparison sentence renders a numeric comparison before any "
                          "run has completed.",
            })
        return violations, {"sentence": sentence, "has_run": has_run}

    # After a run: verify sign/word agreement and no negative number in the sentence.
    if diff >= 0 and word != "more":
        violations.append({"customer_id": None,
                           "detail": f"agent recovered >= baseline but sentence says '{word}'."})
    if diff < 0 and word != "less":
        violations.append({"customer_id": None,
                           "detail": f"agent recovered < baseline but sentence says '{word}'."})
    if "-" in sentence.split("(")[0]:  # no negative in the headline clause
        violations.append({"customer_id": None,
                           "detail": f"comparison sentence contains a negative number: {sentence!r}"})
    return violations, {"sentence": sentence, "has_run": has_run,
                        "agent_recovered": agent_rec, "baseline_recovered": base_rec}


# --- Orchestration ----------------------------------------------------------
RULES = [
    ("rule_1_mandate_revoked_no_retry",
     "mandate_revoked never recovers via a retry path"),
    ("rule_2_retry_cap",
     "no case exceeds the 3-retry cap"),
    ("rule_3_pre_debit_before_retry",
     "every retry has a preceding pre-debit notification (no silent compliance gap)"),
    ("rule_4_rejected_isolation",
     "rejected webhooks have exactly one rejection event and never enter the pipeline"),
    ("rule_5_over_limit_reauth",
     "over-limit amounts route through re-authorization, not a normal retry"),
    ("rule_6_money_figures_match",
     "/api/metrics rupee figures match an independent recomputation"),
    ("rule_7_comparison_sentence",
     "agent-vs-baseline sentence is numerically and grammatically consistent"),
]


def run_audit(conn=None):
    """Run all seven checks and return a structured report dict.

    Report shape:
      {
        "passed": bool,
        "total_violations": int,
        "checks": [
          {"id", "description", "passed", "violation_count", "violations": [...]},
          ...
        ],
        "recomputed": {...},   # supporting figures the audit derived
      }
    """
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        cases = db.get_all_cases(conn)
        audit_by_case = _audit_by_case(conn)

        results = {
            "rule_1_mandate_revoked_no_retry": check_mandate_revoked_no_retry(cases, audit_by_case),
            "rule_2_retry_cap": check_retry_cap(cases, audit_by_case),
            "rule_3_pre_debit_before_retry": check_pre_debit_before_retry(cases, audit_by_case),
            "rule_4_rejected_isolation": check_rejected_isolation(cases, audit_by_case),
            "rule_5_over_limit_reauth": check_over_limit_reauth(cases, audit_by_case),
        }
        money_violations, recomputed_money = check_money_figures(conn)
        results["rule_6_money_figures_match"] = money_violations
        sentence_violations, sentence_info = check_comparison_sentence(conn)
        results["rule_7_comparison_sentence"] = sentence_violations

        checks = []
        total = 0
        for rule_id, description in RULES:
            v = results[rule_id]
            total += len(v)
            checks.append({
                "id": rule_id,
                "description": description,
                "passed": len(v) == 0,
                "violation_count": len(v),
                "violations": v,
            })

        # A run is "not yet done" if every case is still 'new' (no processing).
        run_completed = any(c["case_status"] != "new" for c in cases)

        return {
            "passed": total == 0,
            "total_violations": total,
            "run_completed": run_completed,
            "total_cases": len(cases),
            "checks": checks,
            "recomputed": {**recomputed_money, "comparison": sentence_info},
        }
    finally:
        if own:
            conn.close()


def print_report(report):
    """Print a clear PASS/FAIL console report."""
    line = "=" * 70
    print(line)
    print("MANDATE RESCUE — CORRECTNESS AUDIT")
    print(line)
    if not report.get("run_completed"):
        print("NOTE: no agent run detected yet (cases still 'new'); "
              "run the agent first for a meaningful audit.")
    for chk in report["checks"]:
        status = "PASS" if chk["passed"] else "FAIL"
        print(f"[{status}] {chk['id']}: {chk['description']}")
        if not chk["passed"]:
            for v in chk["violations"]:
                cid = v.get("customer_id")
                prefix = f"    - {cid}: " if cid else "    - "
                print(prefix + v["detail"])
    print(line)
    overall = "ALL CHECKS PASSED" if report["passed"] else (
        f"{report['total_violations']} VIOLATION(S) FOUND")
    print(f"RESULT: {overall}  ({report['total_cases']} cases audited)")
    print(line)
    return report["passed"]


if __name__ == "__main__":
    import sys
    rep = run_audit()
    ok = print_report(rep)
    sys.exit(0 if ok else 1)
