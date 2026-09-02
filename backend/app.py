"""Flask app for Mandate Rescue (design.md section 10).

Serves the dashboard and a small JSON API. Every metric returned here is computed
from real mandate_failures / audit_log rows via metrics.py and baseline.py (N1).
"""

import json
import logging
import os
import uuid

# Load a local .env (project root) into os.environ so GROQ_API_KEY / WEBHOOK_SECRET
# set there are picked up automatically. Optional: if python-dotenv isn't installed
# the app still runs and reads real environment variables as before.
try:
    from dotenv import load_dotenv

    _ENV_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
    )
    load_dotenv(_ENV_PATH)
except ImportError:
    pass

from flask import Flask, jsonify, render_template, request, Response, g

# Configure root logging once so server-side diagnostics (e.g. the real reason an LLM
# call failed inside llm_client._chat) are actually emitted instead of vanishing.
# Level is overridable via LOG_LEVEL for quieter/noisier environments.
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s [%(correlation_id)s]: %(message)s",
)

# Inject a per-request correlation_id into every log record so tracing a specific
# webhook or agent run through the logs is straightforward. Guard with a sentinel so
# importlib.reload() (used in tests) doesn't chain the factory multiple times,
# which would cause infinite recursion.
_CORRELATION_FACTORY_INSTALLED = "_mandate_rescue_correlation_factory"

if not getattr(logging, _CORRELATION_FACTORY_INSTALLED, False):
    _old_factory = logging.getLogRecordFactory()

    def _record_factory(*args, **kwargs):
        record = _old_factory(*args, **kwargs)
        record.correlation_id = getattr(g, "correlation_id", "-") if _in_request_ctx() else "-"
        return record

    def _in_request_ctx():
        try:
            from flask import has_request_context
            return has_request_context()
        except Exception:
            return False

    logging.setLogRecordFactory(_record_factory)
    setattr(logging, _CORRELATION_FACTORY_INSTALLED, True)

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
import simulation_runner
import health as health_module
# Additive ML validation/research layer. Imported defensively so the app still runs
# if the model has not been trained yet (predict.* degrade to "unavailable").
from ml import predict as ml_predict
# Additive SHAP explainability for the ML validation layer. Imported defensively so
# the app still runs if shap isn't installed or the model isn't trained.
try:
    from ml import explain as ml_explain
except Exception:  # pragma: no cover - shap optional
    ml_explain = None
import audit_check as audit_module
import chaos_test as chaos_module
import security
import razorpay_adapter
# Phase 4: scheduler, execution service, Razorpay credential probe.
import scheduler as scheduler_module
import payment_executor as executor_module

# Templates and static assets live in the sibling frontend/ folder.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FRONTEND = os.path.join(_PROJECT_ROOT, "frontend")

app = Flask(
    __name__,
    template_folder=os.path.join(_FRONTEND, "templates"),
    static_folder=os.path.join(_FRONTEND, "static"),
)

# Limit inbound request body size. Protects the webhook endpoint against oversized
# payloads (a real Razorpay webhook is a few KB at most; 1 MB is very generous).
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1 MB

# Security: the ONLY filter fields the natural-language query endpoint will honor.
# The LLM may return anything; every key is validated against this hardcoded set
# before any query is built, and off-list keys are dropped silently. This is a
# closed allow-list, so no arbitrary LLM-chosen field can reach the data layer.
# Must stay in sync with the filters query.run_query() actually honors. Every key
# here is still validated/coerced inside query.py (parameterized SQL, numeric
# coercion, fixed column names), so widening this list adds no injection surface --
# it only stops the endpoint from silently dropping filters the engine supports
# (e.g. "cases over the mandate limit" -> over_limit).
ASK_FIELD_WHITELIST = frozenset({
    "compliance_status", "health_band", "failure_reason", "case_status",
    "amount_min", "amount_max",
    "over_limit", "score_min", "score_max", "dunning_stage_min", "sort_by_amount",
})


# --- Security: API-key gate on mutating endpoints ---------------------------
# Endpoints that change state (reseed/wipe the DB, run the agent, spend Monte Carlo
# compute) require a valid X-API-Key header. Read-only endpoints (GET /api/cases,
# /api/metrics, etc.) are NOT gated — they only ever return data, so the risk profile
# is different, and gating every read would break simple curl-based judge exploration
# for no security benefit. See backend/security.py for the key model.
_PROTECTED_PATHS = frozenset({
    "/api/seed", "/api/run-agent", "/api/run-agent-stream", "/api/reset", "/api/simulate",
    # These are read-only but trigger heavy compute (full DB scans, audit recomputation,
    # or adversarial simulation suites). Gating them prevents unauthenticated abuse.
    "/api/audit-check", "/api/chaos-test",
    # Phase 4: scheduler worker trigger and job cancellation are mutating.
    "/api/scheduler/run", "/api/scheduler/jobs/cancel",
})


@app.before_request
def _assign_correlation_id():
    """Assign a unique correlation ID to each request for log tracing."""
    g.correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())[:8]


@app.before_request
def _require_api_key_for_mutations():
    if request.path not in _PROTECTED_PATHS:
        return None
    supplied = request.headers.get("X-API-Key")
    if not security.is_valid_key(supplied):
        return jsonify({
            "ok": False,
            "error": "unauthorized",
            "message": ("This endpoint requires a valid X-API-Key header. The "
                       "dashboard UI sends this automatically; direct/external "
                       "callers must supply the key configured via "
                       "MANDATE_RESCUE_API_KEY."),
        }), 401
    return None


@app.after_request
def _set_security_headers(response):
    """Add security headers to every response.

    CSP is scoped to same-origin + Google Fonts + jsDelivr CDN (Chart.js). The
    policy blocks inline scripts except the small bootstrap snippet in index.html
    (nonce-less for simplicity; a nonce would require per-request template rendering).
    Adjust 'unsafe-inline' if you move all inline JS out to the static bundle.
    """
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    # Echo the correlation ID in the response so clients/logs can pair them up.
    cid = getattr(g, "correlation_id", None)
    if cid:
        response.headers["X-Correlation-ID"] = cid
    return response


@app.route("/api/_client-key")
def api_client_key():
    """Same-origin bootstrap: hand the dashboard's own JS the current API key.

    This is NOT a security boundary by itself — it's how the UI (running on the
    same origin as the server) picks up the key so normal dashboard use keeps
    working without a login step. An external caller without same-origin access to
    this endpoint still cannot call the protected routes without knowing the key
    from the server's own environment/log.
    """
    return jsonify({"api_key": security.get_api_key()})


@app.errorhandler(413)
def _request_too_large(e):
    return jsonify({"ok": False, "error": "request_too_large",
                    "message": "Request body exceeds the 1 MB limit."}), 413


@app.route("/healthz")
def healthz():
    """Liveness probe: process is up and can reach the database. No auth needed."""
    try:
        conn = db.get_connection()
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
        db_ok = True
    except Exception as e:  # pragma: no cover - defensive
        app.logger.warning("healthz DB check failed: %s", e)
        db_ok = False
    return jsonify({"status": "ok" if db_ok else "degraded", "db_ok": db_ok}), (200 if db_ok else 503)


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


# --- Real Razorpay webhook intake --------------------------------------------
@app.route("/api/webhooks/razorpay", methods=["POST"])
def api_webhook_razorpay():
    """Receive a REAL Razorpay webhook (test or live mode) and feed it into the
    same recovery pipeline used for synthetic data.

    Verification uses Razorpay's actual scheme: HMAC-SHA256 over the RAW request
    body, keyed with RAZORPAY_WEBHOOK_SECRET (configured in the Razorpay Dashboard),
    checked via razorpay_adapter.verify_razorpay_signature — NOT the synthetic
    webhook_security.py scheme used by seed.py, and NOT re-serialized JSON (which
    would silently break the signature). An invalid/missing signature is rejected
    with 400 and never reaches the database or the pipeline.

    Razorpay expects a 2xx response for every delivered event (understood or not) or
    it will keep retrying delivery, so unrecognized event types are acknowledged
    with 200 and simply skipped rather than erroring.
    """
    raw_body = request.get_data()  # exact raw bytes, before any JSON parsing
    signature = request.headers.get("X-Razorpay-Signature")

    if not razorpay_adapter.verify_razorpay_signature(raw_body, signature):
        app.logger.warning("Rejected Razorpay webhook: signature verification failed.")
        return jsonify({"ok": False, "error": "invalid_signature"}), 400

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return jsonify({"ok": False, "error": "invalid_json"}), 400

    record = razorpay_adapter.map_razorpay_event(payload)
    if record is None:
        # Recognized-but-unhandled event type, or missing required fields
        # (e.g. no resolvable customer_id). Acknowledge so Razorpay stops retrying.
        return jsonify({"ok": True, "skipped": True,
                        "event": payload.get("event")}), 200

    if record.get("amount") is None or float(record["amount"]) <= 0:
        # A subscription-level event with no attached charge amount (e.g. a bare
        # subscription.halted with no failed payment yet) carries nothing for the
        # recovery pipeline to act on. Store nothing; acknowledge receipt.
        return jsonify({"ok": True, "skipped": True,
                        "reason": "no_actionable_amount"}), 200

    conn = db.get_connection()
    try:
        is_duplicate, event_id = razorpay_adapter.claim_webhook_event(
            conn, payload, raw_body, record["customer_id"])
        if is_duplicate:
            conn.commit()
            return jsonify({
                "ok": True,
                "status": "already_processed",
                "event_id": event_id,
                "customer_id": record["customer_id"],
            }), 200

        existing = db.get_case(conn, record["customer_id"])
        if existing is not None:
            # Distinct *new* event for a customer already on file (e.g. a later
            # failed retry on the same subscription): update in place rather than
            # violating the customer_id PRIMARY KEY. Duplicate *event ids* never
            # reach here — they returned already_processed above.
            db.update_case(conn, record["customer_id"],
                          amount=record["amount"], failure_reason=record["failure_reason"],
                          raw_event_type=record["raw_event_type"])
            db.mark_webhook_event_processed(conn, event_id)
            conn.commit()
            return jsonify({"ok": True, "updated": True,
                            "customer_id": record["customer_id"],
                            "event_id": event_id}), 200
        db.insert_mandate_failure(conn, record)
        db.mark_webhook_event_processed(conn, event_id)
        conn.commit()
    except Exception as exc:
        # Any unhandled exception during persistence: roll back the entire operation
        # so we leave no partial state. Log the real reason server-side; return a
        # generic 500 so internal details aren't leaked to the caller.
        try:
            conn.rollback()
        except Exception:
            pass
        app.logger.error(
            "Webhook persistence error for event_id=%s customer_id=%s: %s",
            payload.get("id", "unknown"), record.get("customer_id", "unknown"), exc,
            exc_info=True,
        )
        return jsonify({"ok": False, "error": "internal_error",
                        "message": "Webhook could not be persisted. Razorpay will retry."}), 500
    finally:
        conn.close()

    return jsonify({"ok": True, "created": True,
                    "customer_id": record["customer_id"],
                    "failure_reason": record["failure_reason"]}), 200


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


@app.route("/api/ml-feature-importance")
def api_ml_feature_importance():
    """Global SHAP feature importance for the ML validation layer (interpretation only).

    Returns the mean absolute SHAP value per ORIGINAL feature across the held-out test
    set — i.e. which factors matter most to the recovery-likelihood model overall.
    Computed with real SHAP values (see backend/ml/explain.py); nothing is hardcoded.
    If shap isn't installed or the model isn't trained yet, responds with
    {available: false} so the UI can simply hide the chart.

    Like the model itself, this is additive: it explains a NON-DECISION prediction and
    never drives any retry/escalation/compliance behavior.
    """
    if ml_explain is None:
        return jsonify({"available": False,
                        "message": "SHAP is not installed (pip install shap)."})
    gi = ml_explain.global_feature_importance()
    if gi is None:
        return jsonify({"available": False,
                        "message": ("Model not trained or SHAP unavailable. Run "
                                    "`python backend/ml/train_model.py` first.")})
    payload = dict(gi)
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


@app.route("/api/chaos-test")
def api_chaos_test():
    """DIAGNOSTIC/TESTING TOOL — adversarial "chaos" test suite (NOT normal operation).

    Runs the seven adversarial attack scenarios in backend/chaos_test.py — replayed
    webhooks, negative/zero amounts, duplicate customer_ids, clock-skew timestamps,
    malformed LLM responses, webhook-signature edge cases, and extreme volume — and
    returns a PASS/FAIL report.

    IMPORTANT: every scenario runs against its own FRESH, ISOLATED in-memory SQLite
    database seeded independently. This endpoint NEVER reads, writes, or otherwise
    touches the live demo database, and it does not change any agent/scoring/compliance
    behavior. It exists purely to prove the system's defenses hold up under abuse; it is
    not part of the normal recovery pipeline.
    """
    report = chaos_module.run_chaos_suite()
    return jsonify(report)


# Bounds for the Policy Sandbox inputs. retry_cap is clamped to a sane 1-5 range; the
# four score weights must sum to ~1.0. These are validated server-side too (never
# trust the client) before any simulation runs.
_SIM_MIN_RETRY_CAP = 1
_SIM_MAX_RETRY_CAP = 5
_SIM_MAX_RUNS = 100
_SIM_WEIGHT_KEYS = ("success", "tenure", "retry", "reason")


def _parse_policy_params(payload):
    """Validate a sandbox request body into an agent.PolicyParams.

    Returns (policy, error_message). On any validation failure `policy` is None and
    `error_message` explains why, so the endpoint can return a 400 without running
    anything. Enforces retry_cap in [1,5], the four weights summing to 1.0 (+/-0.01),
    and salary_window_mode in {adaptive, generic_only}.
    """
    # retry_cap
    try:
        retry_cap = int(payload.get("retry_cap", agent_module.MAX_RETRIES))
    except (TypeError, ValueError):
        return None, "retry_cap must be an integer between 1 and 5."
    if not (_SIM_MIN_RETRY_CAP <= retry_cap <= _SIM_MAX_RETRY_CAP):
        return None, f"retry_cap must be between {_SIM_MIN_RETRY_CAP} and {_SIM_MAX_RETRY_CAP}."

    # score_weights: default to the live defaults when omitted.
    raw_weights = payload.get("score_weights")
    if raw_weights is None:
        weights = dict(agent_module.DEFAULT_SCORE_WEIGHTS)
    else:
        if not isinstance(raw_weights, dict):
            return None, "score_weights must be an object with success/tenure/retry/reason."
        weights = {}
        for k in _SIM_WEIGHT_KEYS:
            if k not in raw_weights:
                return None, f"score_weights is missing '{k}'."
            try:
                weights[k] = float(raw_weights[k])
            except (TypeError, ValueError):
                return None, f"score_weights['{k}'] must be a number."
            if weights[k] < 0:
                return None, "score weights cannot be negative."
        total = sum(weights[k] for k in _SIM_WEIGHT_KEYS)
        if abs(total - 1.0) > 0.01:
            return None, (f"score weights must sum to 1.0 (got {total:.3f}). "
                          "Adjust the four weights so they add up to 1.0.")

    # salary_window_mode
    mode = payload.get("salary_window_mode", "adaptive")
    if mode not in ("adaptive", "generic_only"):
        return None, "salary_window_mode must be 'adaptive' or 'generic_only'."

    policy = agent_module.PolicyParams(
        retry_cap=retry_cap,
        score_weights=weights,
        salary_window_mode=mode,
    )
    return policy, None


@app.route("/api/simulate", methods=["POST"])
def api_simulate():
    """Policy Experimentation Sandbox: Monte Carlo the pipeline under given params.

    Body: {n_runs, retry_cap, score_weights:{success,tenure,retry,reason},
           salary_window_mode}. Runs the full simulation n_runs times under the given
           policy AND under the current default policy over the same seeds, and returns
           mean/std/95% CI per metric plus the paired delta (so the UI can state an
           improvement as "X% +/- Y%").

    IMPORTANT: this is an analysis tool only. It runs in isolated in-memory databases,
    never touches the live agent's configuration or the on-disk database, and (for
    speed) skips the LLM entirely — narration is template-based and never affects any
    decision, score, or outcome.
    """
    payload = request.get_json(silent=True) or {}

    try:
        n_runs = int(payload.get("n_runs", 30))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "n_runs must be an integer."}), 400
    if n_runs < 1 or n_runs > _SIM_MAX_RUNS:
        return jsonify({"ok": False,
                        "message": f"n_runs must be between 1 and {_SIM_MAX_RUNS}."}), 400

    policy, err = _parse_policy_params(payload)
    if err:
        return jsonify({"ok": False, "message": err}), 400

    # Run modified vs. default over the same seed set for a fair paired comparison.
    comparison = simulation_runner.compare_policies(policy, n_runs=n_runs)
    return jsonify({
        "ok": True,
        "n_runs": n_runs,
        "used_llm": False,
        "note": ("Analysis tool only: repeated simulations in isolated in-memory "
                 "databases. Does not change the live agent's configuration and skips "
                 "the LLM (narration is template-based and never affects outcomes)."),
        "default": comparison["default"],
        "modified": comparison["modified"],
        "delta": comparison["delta"],
    }), 200


@app.route("/api/metrics")
def api_metrics():
    """Core KPIs (R5) plus two baselines for the comparison card (R11).

    'baseline' (naive, 1 attempt, no strategy) is kept for backward compatibility
    with existing callers. 'dumb_persistence' (same retry BUDGET as the agent, but
    no scoring/timing/dunning) isolates the value of the agent's actual intelligence
    from the value of simply retrying more times — see baseline.py's module
    docstring for why this second baseline exists.
    """
    conn = db.get_connection()
    try:
        core = metrics.core_metrics(conn)
        base = baseline.run_baseline(conn)
        dumb = baseline.run_dumb_persistence_baseline(conn)
    finally:
        conn.close()
    return jsonify({"agent": core, "baseline": base, "dumb_persistence": dumb})


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
        # Provenance: 'razorpay_live' for a case that arrived via a real, signature-
        # verified Razorpay webhook (see /api/webhooks/razorpay); 'synthetic' for the
        # seeded demo data. Purely informational — never affects scoring/strategy.
        "source": case.get("source", "synthetic"),
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
    """Full audit trail for one case (R4/R7), plus the case summary + messages +
    state transition history."""
    conn = db.get_connection()
    try:
        case = db.get_case(conn, customer_id)
        if case is None:
            return jsonify({"error": "case not found"}), 404
        summary = _case_summary(case)
        trail = db.get_audit_for_case(conn, customer_id)
        transitions = db.get_state_transitions(conn, customer_id)
        msgs = llm_client.generate_message_variants(case)
    finally:
        conn.close()
    return jsonify({"case": summary, "audit": trail,
                    "transitions": transitions, "messages": msgs})


@app.route("/api/cases/<customer_id>/explain")
def api_case_explain(customer_id):
    """Per-case SHAP breakdown: why the ML model predicts this case's recovery likelihood.

    Returns the top signed feature contributions (positive = pushed toward "recovered",
    negative = toward "not recovered"), the model's base value, and the reconstructed
    probability (base + sum of SHAP values through the sigmoid), which equals the
    model's actual predicted probability — the SHAP additivity property.

    This is the ML validation/interpretability layer. It explains a NON-DECISION
    prediction and never drives any agent/scoring/compliance behavior.
    """
    if ml_explain is None:
        return jsonify({"available": False,
                        "message": "SHAP is not installed (pip install shap)."})
    conn = db.get_connection()
    try:
        case = db.get_case(conn, customer_id)
    finally:
        conn.close()
    if case is None:
        return jsonify({"error": "case not found"}), 404
    breakdown = ml_explain.explain_case(case)
    if breakdown is None:
        return jsonify({"available": False,
                        "message": ("Model not trained or SHAP unavailable. Run "
                                    "`python backend/ml/train_model.py` first.")})
    return jsonify({
        "available": True,
        "customer_id": customer_id,
        "explanation": breakdown,
    })


@app.route("/api/cases/<customer_id>/replay", methods=["POST"])
def api_case_replay(customer_id):
    """Event Replay: re-process a stored case through the recovery pipeline.

    This is a REAL reprocessing mechanism, not a UI animation. It runs the exact same
    DiagnosisAgent → TriageAgent → StrategyAgent → CommunicationAgent pipeline on the
    stored case. The full duplicate-protection chain applies: if the case already has
    a terminal audit record (recovered / escalated / rejected), the pipeline logs a
    webhook_duplicate event and returns without reprocessing — guaranteeing that replay
    can never create duplicate recovery attempts or double-count revenue.

    Returns the case's post-replay state plus its full audit trail so the caller can
    compare before/after. Requires X-API-Key (same gate as /api/run-agent).

    NOTE: replay changes real DB state (status, audit_log, state_transitions). It is
    intended for internal developer/ops use, which is why it is API-key gated.
    """
    supplied = request.headers.get("X-API-Key")
    if not security.is_valid_key(supplied):
        return jsonify({"ok": False, "error": "unauthorized",
                        "message": "X-API-Key required for case replay."}), 401

    import random as _random
    conn = db.get_connection()
    try:
        case = db.get_case(conn, customer_id)
        if case is None:
            return jsonify({"error": "case not found"}), 404

        audit_before = db.get_audit_for_case(conn, customer_id)
        was_terminal = any(
            row.get("case_status_after") in ("recovered", "escalated", "rejected")
            for row in audit_before
        )

        if not was_terminal:
            # Case has not been processed yet — run through the pipeline.
            policy = agent_module.PolicyParams(use_llm=False)
            rng = _random.Random(agent_module.RUN_SEED)
            pipeline = agent_module.RecoveryPipeline(conn, rng, policy=policy)
            pipeline.process_case(dict(case))
            conn.commit()

        case_after = db.get_case(conn, customer_id)
        audit_after = db.get_audit_for_case(conn, customer_id)
        transitions = db.get_state_transitions(conn, customer_id)
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        app.logger.error("Case replay error for %s: %s", customer_id, exc, exc_info=True)
        return jsonify({"ok": False, "error": "internal_error",
                        "message": "Replay failed. See server logs."}), 500
    finally:
        conn.close()

    return jsonify({
        "ok": True,
        "customer_id": customer_id,
        "was_already_terminal": was_terminal,
        "case": _case_summary(case_after),
        "audit": audit_after,
        "transitions": transitions,
    })


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


@app.route("/api/webhook-events")
def api_webhook_events():
    """All stored webhook_events rows (idempotency table), newest first.

    Each row represents one distinct inbound webhook delivery, whether Razorpay-live
    or injected via the demo script. Shows event_id, payload_hash, received_at,
    processed state, customer_id, event_type, and rejected_reason (if any).
    This is the real idempotency table — these are not fabricated for display.
    """
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 500))
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM webhook_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/activity")
def api_activity():
    """Recent audit_log events across all cases, newest first (read-only feed).

    Additive, read-only view over the existing audit_log rows written by the agent.
    Does not change any agent/scoring/compliance behavior; it simply surfaces the
    most recent events for the dashboard's Activity feed. `limit` (default 40) caps
    how many rows are returned.
    """
    try:
        limit = int(request.args.get("limit", 40))
    except (TypeError, ValueError):
        limit = 40
    limit = max(1, min(limit, 200))

    conn = db.get_connection()
    try:
        rows = db.get_all_audit(conn)
    finally:
        conn.close()
    # get_all_audit returns ascending by event_id; take the newest `limit`, newest first.
    recent = list(reversed(rows))[:limit]
    events = [
        {
            "event_id": r.get("event_id"),
            "customer_id": r.get("customer_id"),
            "event_timestamp": r.get("event_timestamp"),
            "event_type": r.get("event_type"),
            "action_taken": r.get("action_taken"),
            "outcome": r.get("outcome"),
            "attempt_number": r.get("attempt_number"),
            "case_status_after": r.get("case_status_after"),
        }
        for r in recent
    ]
    return jsonify({"events": events, "total": len(rows)})


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

    # Security: cap question length to prevent large LLM prompt injection attempts.
    # 500 chars is generous for any natural-language ops question while blocking
    # prompt stuffing or token-flooding attacks.
    _MAX_QUESTION_LEN = 500
    if len(question) > _MAX_QUESTION_LEN:
        return jsonify({"ok": False, "reason": "too_long",
                        "message": f"Question must be {_MAX_QUESTION_LEN} characters or fewer."}), 200

    spec = llm_client.translate_query(question)
    if spec is None:
        # The LLM call itself failed (not a "couldn't understand" case). Tailor the
        # message to the REAL underlying reason so a transient rate-limit reads
        # differently from a hard outage, and log the real cause server-side. The
        # actual error was already logged inside llm_client._chat().
        err = llm_client.last_error()
        if err == llm_client.ERR_RATE_LIMIT:
            reason, msg = ("rate_limited",
                           "The query assistant is temporarily rate-limited. "
                           "Give it a moment and try again.")
        elif err == llm_client.ERR_TIMEOUT:
            reason, msg = ("timeout",
                           "The query assistant took too long to respond. "
                           "Please try again in a moment.")
        elif err == llm_client.ERR_NO_KEY:
            reason, msg = ("unavailable",
                           "The query assistant isn't configured right now. "
                           "Try one of the example queries.")
        else:
            # network / http_error / bad_response / unknown.
            reason, msg = ("unavailable",
                           "The query assistant is temporarily unavailable. "
                           "Please try again shortly, or use one of the examples.")
        app.logger.warning("/api/ask LLM failure: reason=%s underlying=%s question=%r",
                           reason, err, question)
        return jsonify({"ok": False, "reason": reason, "message": msg}), 200
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
    # The live agent run writes to mandate_rescue.db (and its -wal/-journal sidecars)
    # on every run. Flask's default "stat" reloader watches all files under the project,
    # so those writes were triggering a mid-run server restart that forcibly closed the
    # in-flight /api/run-agent-stream SSE connection — making the live pipeline appear to
    # freeze partway. We keep the debugger on but exclude the database files from the
    # reloader's watch list so a run never restarts the server. Source-code edits still
    # trigger a reload as usual.
    _DB_GLOBS = [
        db.DB_PATH,
        db.DB_PATH + "-wal",
        db.DB_PATH + "-journal",
        db.DB_PATH + "-shm",
        os.path.join(_PROJECT_ROOT, "*.db"),
        os.path.join(_PROJECT_ROOT, "*.db-*"),
    ]
    app.run(debug=True, port=5000, threaded=True, exclude_patterns=_DB_GLOBS)
