"""Flask app for Mandate Rescue (design.md section 10).

Serves the dashboard and a small JSON API. Every metric returned here is computed
from real mandate_failures / audit_log rows via metrics.py and baseline.py (N1).
"""

import json
import os

from flask import Flask, jsonify, render_template, request, Response

import db
import seed as seed_module
import agent as agent_module
import scoring
import salary_window
import messaging
import llm_client
import query as query_module
import metrics
import baseline
import export as export_module
import health as health_module
# Additive ML validation/research layer. Imported defensively so the app still runs
# if the model has not been trained yet (predict.* degrade to "unavailable").
from ml import predict as ml_predict
import audit_check as audit_module

# Templates and static assets live in the sibling frontend/ folder.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FRONTEND = os.path.join(_PROJECT_ROOT, "frontend")

app = Flask(
    __name__,
    template_folder=os.path.join(_FRONTEND, "templates"),
    static_folder=os.path.join(_FRONTEND, "static"),
)

# Security: the ONLY filter fields the natural-language query endpoint will honor.
# The LLM may return anything; every key is validated against this hardcoded set
# before any query is built, and off-list keys are dropped silently. This is a
# closed allow-list, so no arbitrary LLM-chosen field can reach the data layer.
ASK_FIELD_WHITELIST = frozenset({
    "compliance_status", "health_band", "failure_reason", "case_status",
    "amount_min", "amount_max",
})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/seed", methods=["POST"])
def api_seed():
    """(Re)generate the 180 synthetic records."""
    count = seed_module.seed_database()
    return jsonify({"seeded": count})


@app.route("/api/run-agent", methods=["POST"])
def api_run_agent():
    """Run the recovery agent over all seeded cases."""
    summary = agent_module.run_agent()
    return jsonify(summary)


@app.route("/api/run-agent-stream")
def api_run_agent_stream():
    """Stream per-case pipeline traces as Server-Sent Events for the live view.

    Each `data:` line is one case's trace (Diagnosis -> Triage -> Strategy ->
    Communication result); a final event carries {done, processed, status_counts}.
    Uses the same seeded RNG / triage order as /api/run-agent, so outcomes match.
    """
    def event_stream():
        for trace in agent_module.run_agent_traced():
            yield "data: " + json.dumps(trace) + "\n\n"

    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Reset the demo: clear any previous run + audit trail and re-seed fresh data.

    Also clears the LLM response cache so a fresh run regenerates narration/messages.
    """
    count = seed_module.seed_database()
    llm_client.clear_cache()
    return jsonify({"reset": True, "seeded": count})


@app.route("/api/status")
def api_status():
    """Lightweight state probe so the UI can show an empty state before seeding."""
    conn = db.get_connection()
    try:
        cases = db.get_all_cases(conn)
        total = len(cases)
        has_run = any(c["case_status"] not in ("new",) for c in cases)
    finally:
        conn.close()
    return jsonify({"seeded": total > 0, "total_cases": total, "has_run": has_run})


@app.route("/api/ml-metrics")
def api_ml_metrics():
    """Real, validated metrics for the additive ML layer (from backend/ml/metrics.json).

    Returns the exact numbers written by train_model.py: precision, recall, F1, AUC,
    confusion matrix, which model won, and the train/test split sizes. Nothing here is
    hardcoded; it is read straight from the metrics.json artifact. If the model has not
    been trained yet, responds with {available: false} so the UI can show a hint.

    This endpoint is informational only. Actual retry/escalation/compliance decisions
    are made by the deterministic rule-based scoring + agent pipeline, not this model.
    """
    metrics_json = ml_predict.load_metrics()
    if metrics_json is None:
        return jsonify({
            "available": False,
            "message": ("Model not trained yet. Run "
                        "`python backend/ml/train_model.py` to generate metrics."),
        })
    payload = dict(metrics_json)
    payload["available"] = True
    return jsonify(payload)


@app.route("/api/audit-check")
def api_audit_check():
    """Automated correctness audit (additive, read-only).

    Re-derives ground truth from audit_log + case_status and verifies the app's seven
    stated business rules hold for every case, then compares independently recomputed
    money figures + the agent-vs-baseline sentence against the live metrics. Returns a
    structured PASS/FAIL report with concrete per-case violations. Never writes to the
    DB and never changes agent/scoring/compliance logic.
    """
    conn = db.get_connection()
    try:
        report = audit_module.run_audit(conn)
    finally:
        conn.close()
    return jsonify(report)


@app.route("/api/metrics")
def api_metrics():
    """Core KPIs (R5) plus the naive baseline for the comparison card (R11)."""
    conn = db.get_connection()
    try:
        core = metrics.core_metrics(conn)
        base = baseline.run_baseline(conn)
    finally:
        conn.close()
    return jsonify({"agent": core, "baseline": base})


def _case_summary(case):
    """Build a case row for the table: score + salary-window badge + R13-R16 fields."""
    score, factors = scoring.score_case(case)
    window = salary_window.infer_window(case)
    return {
        "customer_id": case["customer_id"],
        "amount": float(case["amount"]),
        "failure_reason": case["failure_reason"],
        "merchant_category": case["merchant_category"],
        "customer_tenure_months": case["customer_tenure_months"],
        "past_payment_success_rate": case["past_payment_success_rate"],
        "case_status": case["case_status"],
        "score": score,
        "salary_window_label": window["label"],
        "salary_window_inferred": window["inferred"],
        "raw_event_type": case.get("raw_event_type"),
        "mandate_limit": case.get("mandate_limit"),
        "over_limit": float(case["amount"]) > float(case.get("mandate_limit") or 5000),
        "compliance_status": case.get("compliance_status"),
        "dunning_stage": case.get("dunning_stage", 0),
        "health_score": health_module.health_score(
            case.get("past_payment_success_rate", 0.0), case.get("past_retry_count", 0)),
        "health_band": health_module.health_band(health_module.health_score(
            case.get("past_payment_success_rate", 0.0), case.get("past_retry_count", 0))),
        # Additive, non-decision ML prediction shown alongside the rule-based score.
        # None when the model has not been trained. Never affects agent behavior.
        "ml_recovery_probability": ml_predict.predict_recovery_probability(case),
    }


@app.route("/api/cases")
def api_cases():
    """All cases with score + status, sorted by score descending (triage order)."""
    conn = db.get_connection()
    try:
        cases = [_case_summary(c) for c in db.get_all_cases(conn)]
    finally:
        conn.close()
    cases.sort(key=lambda c: c["score"], reverse=True)
    return jsonify(cases)


@app.route("/api/cases/<customer_id>/audit")
def api_case_audit(customer_id):
    """Full audit trail for one case (R4/R7), plus the case summary + messages."""
    conn = db.get_connection()
    try:
        case = db.get_case(conn, customer_id)
        if case is None:
            return jsonify({"error": "case not found"}), 404
        summary = _case_summary(case)
        trail = db.get_audit_for_case(conn, customer_id)
        msgs = llm_client.generate_message_variants(case)
    finally:
        conn.close()
    return jsonify({"case": summary, "audit": trail, "messages": msgs})


@app.route("/api/cohorts")
def api_cohorts():
    """Recovery-rate breakdown by tenure bucket and merchant_category (R10)."""
    return jsonify(metrics.cohorts())


@app.route("/api/exceptions")
def api_exceptions():
    """First-class exceptions list (R5/N2)."""
    return jsonify(metrics.exceptions())


@app.route("/api/rejected-webhooks")
def api_rejected_webhooks():
    """Webhook events blocked at ingestion for failing HMAC signature verification."""
    return jsonify(metrics.rejected_webhooks())


@app.route("/api/messages/<customer_id>")
def api_messages(customer_id):
    """Generated message variants incl. Hinglish for a case (R9)."""
    channel = request.args.get("channel", "SMS")
    conn = db.get_connection()
    try:
        case = db.get_case(conn, customer_id)
        if case is None:
            return jsonify({"error": "case not found"}), 404
        msgs = llm_client.generate_message_variants(case, channel=channel)
    finally:
        conn.close()
    return jsonify(msgs)


@app.route("/api/health/<customer_id>")
def api_health(customer_id):
    """Per-customer subscription health score (R17, stretch)."""
    conn = db.get_connection()
    try:
        case = db.get_case(conn, customer_id)
        if case is None:
            return jsonify({"error": "case not found"}), 404
        result = health_module.health_for_case(case)
    finally:
        conn.close()
    result["customer_id"] = customer_id
    return jsonify(result)


@app.route("/api/ask", methods=["POST"])
def api_ask():
    """Natural-language query over real cases (LLM interprets intent, SQL executes).

    Flow: LLM translates the question -> filter spec -> parameterized SQL / real
    scoring -> matching cases + a one-line LLM summary. If the LLM is unavailable or
    the question can't be turned into any filter, respond gracefully so the UI can
    prompt the user to try an example instead of erroring.
    """
    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify({"ok": False, "reason": "empty",
                        "message": "Type a question, or click one of the examples."}), 200

    spec = llm_client.translate_query(question)
    if spec is None:
        # LLM unavailable/failed entirely.
        return jsonify({"ok": False, "reason": "unavailable",
                        "message": "The query assistant is unavailable right now. "
                                   "Try one of the example queries."}), 200
    if not spec:
        # Understood the call but produced no usable filters.
        return jsonify({"ok": False, "reason": "unclear",
                        "message": "Couldn't understand that one — try one of the "
                                   "example queries below."}), 200

    # Security: enforce a hardcoded field whitelist on the LLM's output BEFORE any
    # query is built. Any key the model returns that is not on this list is dropped
    # silently. Only these field names ever reach query.py, and even there they are
    # used as fixed column names with parameterized values (never string-concatenated
    # LLM text). This is the trust boundary: the LLM picks from a closed set, code runs.
    safe_spec = {k: v for k, v in spec.items() if k in ASK_FIELD_WHITELIST}
    if not safe_spec:
        # Every field the LLM produced was off-whitelist -> treat as not understood.
        return jsonify({"ok": False, "reason": "unclear",
                        "message": "Couldn't understand that one — try one of the "
                                   "example queries below."}), 200

    rows, applied = query_module.run_query(safe_spec)
    summary = llm_client.summarize_results(question, len(rows), rows[:5])
    results = [_case_summary_from_row(r) for r in rows]
    return jsonify({
        "ok": True,
        "question": question,
        "filter": applied,
        "count": len(results),
        "summary": summary,
        "results": results,
    }), 200


def _case_summary_from_row(row):
    """Compact result row for the ask panel (uses computed score/band already set)."""
    return {
        "customer_id": row["customer_id"],
        "amount": float(row["amount"]),
        "failure_reason": row["failure_reason"],
        "merchant_category": row["merchant_category"],
        "case_status": row["case_status"],
        "compliance_status": row.get("compliance_status"),
        "dunning_stage": row.get("dunning_stage", 0),
        "score": row.get("score"),
        "health_band": row.get("health_band"),
        "over_limit": row.get("over_limit", False),
    }


@app.route("/api/export")
def api_export():
    """CSV summary download (R12)."""
    csv_text = export_module.build_summary_csv()
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=mandate_rescue_summary.csv"},
    )


if __name__ == "__main__":
    db.init_db()
    app.run(debug=True, port=5000)
