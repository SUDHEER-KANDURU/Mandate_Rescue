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
# Phase 5: intelligence + adaptive modules (imported defensively)
try:
    import intelligence as intelligence_module
    import risk_engine as risk_engine_module
    import adaptive_policy as adaptive_policy_module
    import economic_value as economic_value_module
    import anomaly_detector as anomaly_detector_module
    _P5_AVAILABLE = True
except Exception as _p5_err:
    _P5_AVAILABLE = False
    import logging as _logging
    _logging.getLogger("mandate_rescue.app").warning(
        "Phase 5 modules not fully available: %s", _p5_err
    )

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


# Phase 4: reset any jobs stuck in 'claimed'/'executing' on first request after
# startup (workers may have died mid-flight). Safe to call repeatedly — it only
# touches rows older than STALE_CLAIMED_WINDOW_MIN.
_stale_jobs_reset = False


@app.before_request
def _reset_stale_jobs_once():
    global _stale_jobs_reset
    if not _stale_jobs_reset:
        _stale_jobs_reset = True
        try:
            scheduler_module.reset_stale_claimed_jobs()
        except Exception as exc:
            app.logger.warning("Could not reset stale jobs on startup: %s", exc)


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


def _case_summary(case, ml_prob=None):
    """Build a case row for the table: score + salary-window badge + R13-R16 fields.

    ``ml_prob`` is pre-computed by ``api_cases`` via the batch predictor to avoid
    the O(N × DataFrame-construction) bottleneck.  When called for a single case
    (e.g. /api/cases/<id>/audit) it falls back to the per-case predictor.
    """
    score, factors = scoring.score_case(case)
    window = salary_window.infer_window(case)
    # Compute health score once; reuse for both health_score and health_band fields.
    h_score = health_module.health_score(
        case.get("past_payment_success_rate", 0.0), case.get("past_retry_count", 0))
    if ml_prob is None:
        ml_prob = ml_predict.predict_recovery_probability(case)
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
        "health_score": h_score,
        "health_band": health_module.health_band(h_score),
        # Additive, non-decision ML prediction shown alongside the rule-based score.
        # None when the model has not been trained. Never affects agent behavior.
        "ml_recovery_probability": ml_prob,
        # Provenance: 'razorpay_live' for a case that arrived via a real, signature-
        # verified Razorpay webhook (see /api/webhooks/razorpay); 'synthetic' for the
        # seeded demo data. Purely informational — never affects scoring/strategy.
        "source": case.get("source", "synthetic"),
    }


@app.route("/api/cases")
def api_cases():
    """All cases with score + status, sorted by score descending (triage order).

    Performance: ML predictions are computed once per request via predict_batch()
    (single DataFrame + single model.predict_proba call) rather than N individual
    per-case calls. health_score is computed once per case instead of twice.
    """
    conn = db.get_connection()
    try:
        raw_cases = db.get_all_cases(conn)
    finally:
        conn.close()
    # Batch ML predictions: one call for all N cases instead of N individual calls.
    ml_probs = ml_predict.predict_batch(raw_cases)
    cases = [_case_summary(c, ml_prob=p) for c, p in zip(raw_cases, ml_probs)]
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

    Performance: uses a single DESC LIMIT query instead of loading all rows into
    Python and slicing.
    """
    try:
        limit = int(request.args.get("limit", 40))
    except (TypeError, ValueError):
        limit = 40
    limit = max(1, min(limit, 200))

    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY event_id DESC LIMIT ?", (limit,)
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    finally:
        conn.close()
    # Rows come back newest-first; present as-is (no Python reversal needed).
    events = [
        {
            "event_id": r["event_id"],
            "customer_id": r["customer_id"],
            "event_timestamp": r["event_timestamp"],
            "event_type": r["event_type"],
            "action_taken": r["action_taken"],
            "outcome": r["outcome"],
            "attempt_number": r["attempt_number"],
            "case_status_after": r["case_status_after"],
        }
        for r in rows
    ]
    return jsonify({"events": events, "total": total})


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



# =============================================================================
# PHASE 4 — Scheduler / Execution Status / Credential Verification endpoints
# =============================================================================


@app.route("/api/scheduler/run", methods=["POST"])
def api_scheduler_run():
    """Claim and execute all currently due recovery jobs in one synchronous pass.

    Protected by X-API-Key (same gate as /api/run-agent).  Safe to call from
    a cron job, a CI pipeline, or the dashboard "Run scheduler" button.

    Returns a list of execution summaries — one per job executed — so the caller
    can see exactly what happened and check no fake outcomes are present.

    Response:
        { "ok": true, "executed": N,
          "results": [{ job_id, customer_id, attempt, outcome,
                        success, execution_mode, job_status,
                        razorpay_payment_id, payment_link_url }, ...] }
    """
    results = scheduler_module.run_worker_once()
    return jsonify({"ok": True, "executed": len(results), "results": results})


@app.route("/api/scheduler/jobs")
def api_scheduler_jobs():
    """Return all recovery jobs (newest first).  Read-only, no auth required.

    Optional query params:
        customer_id   — filter to one customer
        status        — filter by job status
        limit         — max rows (default 200, max 1000)
    """
    try:
        limit = min(int(request.args.get("limit", 200)), 1000)
    except (TypeError, ValueError):
        limit = 200

    customer_id = request.args.get("customer_id")
    status_filter = request.args.get("status")

    conn = db.get_connection()
    try:
        if customer_id:
            jobs = db.get_jobs_for_case(conn, customer_id)
        else:
            jobs = db.get_all_jobs(conn, limit=limit)
    finally:
        conn.close()

    if status_filter:
        jobs = [j for j in jobs if j.get("status") == status_filter]

    return jsonify({"jobs": jobs, "count": len(jobs)})


@app.route("/api/scheduler/jobs/<job_id>")
def api_scheduler_job_detail(job_id):
    """Return a single recovery job row by job_id."""
    conn = db.get_connection()
    try:
        job = db.get_job(conn, job_id)
    finally:
        conn.close()
    if job is None:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job)


@app.route("/api/scheduler/jobs/cancel", methods=["POST"])
def api_scheduler_cancel_job():
    """Cancel a scheduled (not yet executed) recovery job.

    Body: { "job_id": "...", "reason": "optional reason string" }
    Protected by X-API-Key.
    """
    payload = request.get_json(silent=True) or {}
    job_id = (payload.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "message": "job_id is required."}), 400
    reason = (payload.get("reason") or "Cancelled via API.").strip()

    conn = db.get_connection()
    try:
        job = db.get_job(conn, job_id)
        if job is None:
            return jsonify({"ok": False, "message": f"Job {job_id} not found."}), 404
        if job["status"] in (db.JOB_STATUS_SUCCEEDED, db.JOB_STATUS_FAILED,
                             db.JOB_STATUS_CANCELLED, db.JOB_STATUS_EXHAUSTED):
            return jsonify({
                "ok": False,
                "message": f"Job {job_id} is already terminal (status={job['status']})."
            }), 409
        db.cancel_job(conn, job_id, reason=reason)
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "job_id": job_id, "status": "cancelled", "reason": reason})


@app.route("/api/execution/status")
def api_execution_status():
    """Phase 4 execution status summary — pending jobs, mode breakdown, cred check.

    Returns a snapshot useful for the dashboard's execution panel:
        {
          "razorpay": { configured, reachable, authenticated, mode, error },
          "jobs": { total, scheduled, succeeded, failed, exhausted, by_mode },
          "execution_modes": { real_test_cases, simulation_cases }
        }
    No sensitive values are exposed.
    """
    # Credential probe (read-only, never raises).
    creds = executor_module.verify_razorpay_credentials()

    conn = db.get_connection()
    try:
        all_jobs = db.get_all_jobs(conn, limit=5000)
    finally:
        conn.close()

    by_status: dict = {}
    by_mode: dict = {}
    for j in all_jobs:
        s = j.get("status", "unknown")
        m = j.get("execution_mode", "simulation")
        by_status[s] = by_status.get(s, 0) + 1
        by_mode[m] = by_mode.get(m, 0) + 1

    return jsonify({
        "razorpay": {
            "configured":    creds.get("configured", False),
            "reachable":     creds.get("reachable", False),
            "authenticated": creds.get("authenticated", False),
            "mode":          creds.get("mode", "unknown"),
            "error":         creds.get("error"),
        },
        "jobs": {
            "total":     len(all_jobs),
            "scheduled": by_status.get(db.JOB_STATUS_SCHEDULED, 0),
            "claimed":   by_status.get(db.JOB_STATUS_CLAIMED, 0),
            "executing": by_status.get(db.JOB_STATUS_EXECUTING, 0),
            "succeeded": by_status.get(db.JOB_STATUS_SUCCEEDED, 0),
            "failed":    by_status.get(db.JOB_STATUS_FAILED, 0),
            "exhausted": by_status.get(db.JOB_STATUS_EXHAUSTED, 0),
            "cancelled": by_status.get(db.JOB_STATUS_CANCELLED, 0),
            "by_mode":   by_mode,
        },
    })


@app.route("/api/execution/verify-credentials")
def api_execution_verify_credentials():
    """Probe Razorpay Test Mode credentials live and return the result.

    Makes a single lightweight read-only API call (GET /plans?count=1).
    Never exposes the key values — only configured/reachable/authenticated booleans.
    """
    result = executor_module.verify_razorpay_credentials()
    # Determine overall readiness.
    result["ready_for_real_execution"] = (
        result.get("configured", False) and result.get("authenticated", False)
    )
    return jsonify(result)


@app.route("/api/cases/<customer_id>/jobs")
def api_case_jobs(customer_id):
    """Return all recovery jobs for a specific case.

    Used by the case-detail drawer to show the full execution history
    (attempt number, mode, outcome, Razorpay IDs, payment link URL).
    """
    conn = db.get_connection()
    try:
        case = db.get_case(conn, customer_id)
        if case is None:
            return jsonify({"error": "case not found"}), 404
        jobs = db.get_jobs_for_case(conn, customer_id)
    finally:
        conn.close()
    return jsonify({"customer_id": customer_id, "jobs": jobs})



# =============================================================================
# PHASE 5 — Intelligence, Risk, Anomaly, Investigate endpoints
# =============================================================================

def _p5_unavailable():
    return jsonify({
        "ok": False,
        "error": "phase5_unavailable",
        "message": "Phase 5 intelligence modules are not available.",
    }), 503


@app.route("/api/intelligence/summary")
def api_intelligence_summary():
    """Full intelligence summary: strategy outcomes, failure-reason stats,
    counterfactual revenue, merchant-specific learning — in one call.

    Optional query param: simulation=false to skip Monte Carlo strategy comparison
    (faster, suitable for the live dashboard overview).
    """
    if not _P5_AVAILABLE:
        return _p5_unavailable()
    include_sim = request.args.get("simulation", "false").lower() != "false"
    conn = db.get_connection()
    try:
        summary = intelligence_module.full_summary(conn, include_simulation=include_sim)
    finally:
        conn.close()
    return jsonify(summary)


@app.route("/api/intelligence/by-failure-reason")
def api_intelligence_failure_reason():
    """Actual recovery rates per failure reason + comparison to scoring.py priors."""
    if not _P5_AVAILABLE:
        return _p5_unavailable()
    conn = db.get_connection()
    try:
        result = intelligence_module.by_failure_reason(conn)
    finally:
        conn.close()
    return jsonify(result)


@app.route("/api/intelligence/by-strategy")
def api_intelligence_strategy():
    """Actual recovery outcomes per strategy selected by the agent."""
    if not _P5_AVAILABLE:
        return _p5_unavailable()
    conn = db.get_connection()
    try:
        result = intelligence_module.by_strategy_outcome(conn)
    finally:
        conn.close()
    return jsonify(result)


@app.route("/api/intelligence/incremental-revenue")
def api_intelligence_incremental():
    """Counterfactual revenue analysis: actual vs naive baseline vs dumb persistence.
    Baseline values are simulation estimates — clearly labelled in response.
    """
    if not _P5_AVAILABLE:
        return _p5_unavailable()
    conn = db.get_connection()
    try:
        result = intelligence_module.incremental_revenue(conn)
    finally:
        conn.close()
    return jsonify(result)


@app.route("/api/intelligence/merchant-learning")
def api_intelligence_merchant():
    """Per-merchant-category best strategy from actual historical outcomes.
    Only surfaces recommendations for merchants with sufficient sample size.
    """
    if not _P5_AVAILABLE:
        return _p5_unavailable()
    conn = db.get_connection()
    try:
        result = intelligence_module.merchant_learning(conn)
    finally:
        conn.close()
    return jsonify(result)


@app.route("/api/risk/summary")
def api_risk_summary():
    """Revenue-at-risk prediction: top N at-risk cases with risk scores + factors.

    Query params:
        limit (int, default 10) — number of top-risk cases to return
        include_recovered (bool, default false) — include already-recovered cases
    """
    if not _P5_AVAILABLE:
        return _p5_unavailable()
    try:
        limit = int(request.args.get("limit", 10))
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 100))
    include_recovered = request.args.get("include_recovered", "false").lower() == "true"

    conn = db.get_connection()
    try:
        if include_recovered or limit > 10:
            full = risk_engine_module.revenue_at_risk(conn, include_recovered=include_recovered)
            full["cases"] = full["cases"][:limit]
            result = full
        else:
            result = risk_engine_module.top_risks(conn, limit=limit)
    finally:
        conn.close()
    return jsonify(result)


@app.route("/api/risk/case/<customer_id>")
def api_risk_case(customer_id):
    """Risk score + contributing factors for a single case."""
    if not _P5_AVAILABLE:
        return _p5_unavailable()
    conn = db.get_connection()
    try:
        case = db.get_case(conn, customer_id)
        if case is None:
            return jsonify({"error": "case not found"}), 404
        result = risk_engine_module.score_case_risk(case)
    finally:
        conn.close()
    return jsonify(result)


@app.route("/api/adaptive-policy/recommend/<customer_id>")
def api_adaptive_policy_recommend(customer_id):
    """Data-driven strategy recommendation for a single case with explanation."""
    if not _P5_AVAILABLE:
        return _p5_unavailable()
    conn = db.get_connection()
    try:
        case = db.get_case(conn, customer_id)
        if case is None:
            return jsonify({"error": "case not found"}), 404
        result = adaptive_policy_module.recommend_strategy(case, conn)
    finally:
        conn.close()
    return jsonify(result)


@app.route("/api/adaptive-policy/summary")
def api_adaptive_policy_summary():
    """Current adaptive policy state: observed strategy performance + governance config."""
    if not _P5_AVAILABLE:
        return _p5_unavailable()
    conn = db.get_connection()
    try:
        result = adaptive_policy_module.policy_summary(conn)
    finally:
        conn.close()
    return jsonify(result)


@app.route("/api/economic-value/portfolio")
def api_ev_portfolio():
    """Portfolio expected value: total E[net recovery] across all active cases."""
    if not _P5_AVAILABLE:
        return _p5_unavailable()
    conn = db.get_connection()
    try:
        result = economic_value_module.portfolio_ev(conn)
    finally:
        conn.close()
    return jsonify(result)


@app.route("/api/economic-value/case/<customer_id>")
def api_ev_case(customer_id):
    """Expected net value + incremental value vs baseline for a single case."""
    if not _P5_AVAILABLE:
        return _p5_unavailable()
    conn = db.get_connection()
    try:
        case = db.get_case(conn, customer_id)
        if case is None:
            return jsonify({"error": "case not found"}), 404
        from adaptive_policy import _rule_based_strategy
        strategy = _rule_based_strategy(case)
        ev = economic_value_module.expected_value(case, strategy)
        incremental = economic_value_module.incremental_value(case)
    finally:
        conn.close()
    return jsonify({
        "customer_id": customer_id,
        "expected_value": ev,
        "incremental_value": incremental,
    })


@app.route("/api/anomalies")
def api_anomalies():
    """Run anomaly detection on current data and return all active alerts.

    Returns alerts sorted by severity (critical first). Each alert includes
    observed value, expected baseline, affected segment, and recommended action.
    All values derived from real stored data — no hardcoded thresholds.
    """
    if not _P5_AVAILABLE:
        return _p5_unavailable()
    conn = db.get_connection()
    try:
        result = anomaly_detector_module.run_anomaly_detection(conn)
    finally:
        conn.close()
    return jsonify(result)


@app.route("/api/investigate", methods=["POST"])
def api_investigate():
    """Revenue Investigator — upgraded Ask the Data with intelligence context.

    Answers analytical questions using real stored data + intelligence aggregates.
    The response includes:
      - answer: direct answer from real data
      - evidence: supporting metrics from real aggregations
      - segment: most affected segment
      - recommendation: actionable next step

    Questions answered deterministically (no LLM needed for well-formed queries):
      - "why did recovery fall" / "recovery performance"
      - "which failure type" / "most lost revenue"
      - "which strategy performs best"
      - "what revenue is at risk"
      - "anomalies" / "what is failing"

    Falls back to the existing LLM-backed /api/ask behaviour for freeform queries.

    Body: { "question": "..." }
    """
    if not _P5_AVAILABLE:
        return _p5_unavailable()

    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip().lower()
    if not question:
        return jsonify({"ok": False, "reason": "empty",
                        "message": "Provide a question."}), 400

    conn = db.get_connection()
    try:
        answer = _investigate_question(question, conn)
    finally:
        conn.close()

    return jsonify(answer)


def _investigate_question(question: str, conn) -> dict:
    """Route analytical questions to the appropriate intelligence function.

    Returns a structured answer dict. Always uses real stored data.
    """
    q = question.lower()

    # --- Strategy performance (checked BEFORE recovery to avoid "recovery strategy" ambiguity) ---
    if any(k in q for k in ("strategy", "best strategy", "which strategy", "performs best")):
        strat_data = intelligence_module.by_strategy_outcome(conn)
        strategies = [s for s in strat_data["by_strategy"] if s["sufficient_sample"]]
        best = max(strategies, key=lambda s: s["recovery_rate"]) if strategies else None
        return {
            "ok": True,
            "question_type": "strategy_performance",
            "answer": (
                f"Best-performing strategy: '{best['strategy']}' "
                f"({best['recovery_rate']*100:.1f}% recovery on {best['total']} cases, "
                f"Rs {best['amount_recovered']:,.0f} recovered)."
                if best else "Insufficient data for strategy comparison yet."
            ),
            "evidence": {"by_strategy": strat_data["by_strategy"]},
            "segment": best["strategy"] if best else "unknown",
            "recommendation": (
                f"Prioritise '{best['strategy']}' for applicable cases."
                if best else "Run the agent to gather strategy outcome data."
            ),
            "data_type": "actual",
        }

    # --- Recovery performance / why did recovery fall ---
    if any(k in q for k in ("recovery", "recover", "performance", "why did")):
        intel = intelligence_module.by_failure_reason(conn)
        by_reason = intel["by_failure_reason"]
        worst = min(by_reason, key=lambda r: r["recovery_rate"]) if by_reason else None
        incremental = intelligence_module.incremental_revenue(conn)
        return {
            "ok": True,
            "question_type": "recovery_performance",
            "answer": (
                f"Overall recovery rate: "
                f"{incremental['actual']['recovery_rate']*100:.1f}%. "
                + (
                    f"Lowest recovery by failure reason: '{worst['segment']}' "
                    f"at {worst['recovery_rate']*100:.1f}% ({worst['total']} cases, "
                    f"Rs {worst['amount_lost']:,.0f} lost)."
                    if worst else ""
                )
            ),
            "evidence": {
                "actual": incremental["actual"],
                "by_failure_reason": by_reason,
                "incremental_vs_naive": incremental["incremental"],
            },
            "segment": worst["segment"] if worst else "all",
            "recommendation": (
                f"Focus on '{worst['segment']}' cases — "
                f"highest unrecovered amount (Rs {worst['amount_lost']:,.0f})."
                if worst else "Review overall escalation rate."
            ),
            "data_type": "actual",
        }

    # --- Failure type / most lost revenue ---
    if any(k in q for k in ("failure", "lost revenue", "which type", "fail")):
        intel = intelligence_module.by_failure_reason(conn)
        by_reason = sorted(intel["by_failure_reason"],
                           key=lambda r: r["amount_lost"], reverse=True)
        top = by_reason[0] if by_reason else None
        return {
            "ok": True,
            "question_type": "failure_analysis",
            "answer": (
                f"The failure type causing the most lost revenue is "
                f"'{top['segment']}' with Rs {top['amount_lost']:,.0f} unrecovered "
                f"across {top['total']} cases ({(1-top['recovery_rate'])*100:.1f}% failure rate)."
                if top else "No failure data available yet."
            ),
            "evidence": {"by_failure_reason": by_reason},
            "segment": top["segment"] if top else "unknown",
            "recommendation": (
                f"Review strategy for '{top['segment']}' cases. "
                f"Current recovery rate: {top['recovery_rate']*100:.1f}%."
                if top else ""
            ),
            "data_type": "actual",
        }

    # --- Revenue at risk ---
    if any(k in q for k in ("at risk", "risk", "revenue at risk", "how much")):
        risk = risk_engine_module.top_risks(conn, limit=5)
        return {
            "ok": True,
            "question_type": "revenue_at_risk",
            "answer": (
                f"Rs {risk['total_amount_at_risk']:,.0f} is currently at risk "
                f"across {risk['active_cases']} active cases. "
                f"Estimated unrecovered: Rs {risk['expected_unrecovered']:,.0f} "
                f"[ESTIMATE — risk-score weighted]."
            ),
            "evidence": {
                "total_amount_at_risk": risk["total_amount_at_risk"],
                "expected_unrecovered": risk["expected_unrecovered"],
                "active_cases": risk["active_cases"],
                "top_risks": risk["top_risks"],
                "summary_by_severity": risk["summary_by_severity"],
            },
            "segment": "all active cases",
            "recommendation": (
                "Focus recovery efforts on critical-severity cases first "
                "(mandate_revoked and high-value over-limit cases)."
            ),
            "data_type": "mixed",
        }

    # --- Anomalies / what is failing ---
    if any(k in q for k in ("anomal", "unusual", "failing", "degrading", "spike", "alert")):
        anomalies = anomaly_detector_module.run_anomaly_detection(conn)
        critical = [a for a in anomalies["alerts"] if a["severity"] == "critical"]
        warnings = [a for a in anomalies["alerts"] if a["severity"] == "warning"]
        top_alert = anomalies["alerts"][0] if anomalies["alerts"] else None
        return {
            "ok": True,
            "question_type": "anomaly_report",
            "answer": (
                f"{anomalies['total']} alert(s) detected: "
                f"{len(critical)} critical, {len(warnings)} warnings. "
                + (f"Top alert: {top_alert['title']} — {top_alert['description']}"
                   if top_alert else "No anomalies detected.")
            ),
            "evidence": {"alerts": anomalies["alerts"]},
            "segment": top_alert["affected_segment"] if top_alert else "none",
            "recommendation": (
                top_alert["recommended_action"]
                if top_alert else "No action needed."
            ),
            "data_type": "actual",
        }

    # --- What should we change / recommendations ---
    if any(k in q for k in ("recommend", "what should", "change", "improve", "action")):
        policy = adaptive_policy_module.policy_summary(conn)
        merchant = intelligence_module.merchant_learning(conn)
        changes = policy.get("recommended_changes", [])
        return {
            "ok": True,
            "question_type": "recommendations",
            "answer": (
                f"{len(changes)} strategy recommendation(s) based on observed outcomes. "
                + (changes[0]["recommendation"] if changes else
                   "No strategy changes recommended — current performance within expected range.")
            ),
            "evidence": {
                "recommended_changes": changes,
                "merchant_learning": merchant["merchants"],
            },
            "segment": changes[0]["strategy"] if changes else "all",
            "recommendation": changes[0]["recommendation"] if changes else "Maintain current policy.",
            "data_type": "actual",
        }

    # --- Fallback: route to the existing LLM-backed ask endpoint ---
    import query as query_module
    # Build a minimal spec from keywords in the question
    spec = {}
    for reason in ("insufficient_funds", "mandate_expired", "mandate_revoked", "bank_technical_error"):
        if reason.replace("_", " ") in question or reason in question:
            spec["failure_reason"] = reason
            break
    for cat in ("subscription", "emi", "insurance", "utility"):
        if cat in question:
            spec["merchant_category"] = cat
            break

    if spec:
        rows, applied = query_module.run_query(spec, limit=20)
        return {
            "ok": True,
            "question_type": "filtered_cases",
            "answer": f"{len(rows)} cases match your query.",
            "evidence": {"filter": applied, "count": len(rows)},
            "results": [_case_summary_from_row(r) for r in rows[:10]],
            "segment": str(spec),
            "recommendation": "Review the matching cases in the Cases view.",
            "data_type": "actual",
        }

    return {
        "ok": False,
        "question_type": "unknown",
        "answer": (
            "I couldn't interpret that question. Try asking about: "
            "'recovery performance', 'revenue at risk', 'which strategy performs best', "
            "'anomalies', or 'what should we change'."
        ),
        "data_type": "n/a",
    }


# =============================================================================
# PHASE 6 — Closed-Loop Learning endpoints
# =============================================================================

# Import Phase 6 modules defensively so the app still runs even if a module
# has a syntax error (useful during iterative development).
try:
    import outcome_attribution as _oa
    import experimentation as _exp
    import experiment_evaluator as _eval
    import segment_learning as _seg
    import policy_engine as _policy
    import strategy_drift as _drift
    _P6_AVAILABLE = True
except Exception as _p6_err:
    _P6_AVAILABLE = False
    import logging as _logging
    _logging.getLogger("mandate_rescue.app").warning(
        "Phase 6 modules not fully available: %s", _p6_err
    )

# Add Phase 6 protected paths
_PROTECTED_PATHS = _PROTECTED_PATHS | frozenset({
    "/api/learning/backfill",
    "/api/learning/recommendations/generate",
    "/api/learning/recommendations/approve",
    "/api/learning/recommendations/reject",
    "/api/learning/policy/rollback",
    "/api/learning/policy/measure",
    "/api/learning/experiments/create",
    "/api/learning/experiments/complete",
})


def _p6_unavailable():
    return jsonify({
        "ok": False,
        "error": "phase6_unavailable",
        "message": "Phase 6 learning modules are not available.",
    }), 503


# --- Outcome Attribution ---

@app.route("/api/learning/attribution/summary")
def api_learning_attribution_summary():
    """Summary of outcome attribution coverage and data provenance breakdown."""
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    conn = db.get_connection()
    try:
        result = _oa.get_attribution_summary(conn)
    finally:
        conn.close()
    return jsonify(result)


@app.route("/api/learning/backfill", methods=["POST"])
def api_learning_backfill():
    """Backfill strategy_performance from all existing resolved cases.

    Safe to run multiple times (idempotent upsert semantics).
    Also runs outcome_attribution for all terminal cases and records
    experiment outcomes for any assigned cases.
    Protected by X-API-Key.
    """
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    conn = db.get_connection()
    try:
        # 1. Backfill strategy_performance
        backfill = _oa.backfill_from_audit(conn)
        # 2. Record experiment outcomes for all terminal cases
        exp_recorded = 0
        for exp in db.get_all_experiments(conn, status="active"):
            result = _exp.record_all_terminal_outcomes(conn, exp["experiment_id"])
            exp_recorded += result.get("recorded", 0)
        # 3. Measure current active policy performance
        perf = _policy.record_current_policy_performance(conn)
    finally:
        conn.close()
    return jsonify({
        "ok": True,
        "attribution_backfill": backfill,
        "experiment_outcomes_recorded": exp_recorded,
        "policy_performance": perf,
    })


# --- Strategy Performance ---

@app.route("/api/learning/strategy-performance")
def api_learning_strategy_performance():
    """All strategy performance records, optionally filtered by dimension."""
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    dimension_key = request.args.get("dimension_key")
    dimension_value = request.args.get("dimension_value")
    provenance = request.args.get("provenance")

    conn = db.get_connection()
    try:
        rows = db.get_strategy_performance(
            conn,
            dimension_key=dimension_key or None,
            dimension_value=dimension_value or None,
            provenance=provenance or None,
        )
        # Enrich with computed recovery_rate
        for r in rows:
            attempts = r.get("attempts", 0)
            r["recovery_rate"] = round(r.get("recoveries", 0) / attempts, 4) if attempts else 0.0
    finally:
        conn.close()
    return jsonify({"strategy_performance": rows, "count": len(rows)})


@app.route("/api/learning/segment-learning")
def api_learning_segment():
    """Full strategy learning summary across all dimensions with fallback hierarchy."""
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    conn = db.get_connection()
    try:
        result = _seg.full_learning_summary(conn)
    finally:
        conn.close()
    return jsonify(result)


@app.route("/api/learning/strategy-drift")
def api_learning_drift():
    """Detect strategy performance degradation over the recent window."""
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    conn = db.get_connection()
    try:
        result = _drift.detect_strategy_drift(conn)
    finally:
        conn.close()
    return jsonify(result)


# --- Experiments ---

@app.route("/api/learning/experiments", methods=["GET"])
def api_learning_experiments_list():
    """List all experiments with status and arm sample sizes."""
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    status_filter = request.args.get("status")
    conn = db.get_connection()
    try:
        exps = db.get_all_experiments(conn, status=status_filter or None)
        result = []
        for exp in exps:
            status_info = _exp.get_experiment_status(conn, exp["experiment_id"])
            result.append(status_info)
    finally:
        conn.close()
    return jsonify({"experiments": result, "count": len(result)})


@app.route("/api/learning/experiments/create", methods=["POST"])
def api_learning_experiment_create():
    """Create a new A/B experiment.

    Body: {
      name, control_strategy, treatment_strategy,
      description?, merchant_category?, failure_reason?,
      min_sample_size?, created_by?
    }
    Protected by X-API-Key.
    """
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    payload = request.get_json(silent=True) or {}
    required = ("name", "control_strategy", "treatment_strategy")
    for f in required:
        if not payload.get(f):
            return jsonify({"ok": False, "message": f"'{f}' is required."}), 400

    conn = db.get_connection()
    try:
        experiment_id = _exp.create_experiment(
            conn,
            name=payload["name"],
            control_strategy=payload["control_strategy"],
            treatment_strategy=payload["treatment_strategy"],
            description=payload.get("description", ""),
            merchant_category=payload.get("merchant_category"),
            failure_reason=payload.get("failure_reason"),
            min_sample_size=int(payload.get("min_sample_size", 10)),
            created_by=payload.get("created_by", "api"),
        )
        # Auto-assign existing eligible cases
        assignment = _exp.assign_cases(conn, experiment_id)
    finally:
        conn.close()
    return jsonify({
        "ok": True,
        "experiment_id": experiment_id,
        "assignment": assignment,
    }), 201


@app.route("/api/learning/experiments/<experiment_id>")
def api_learning_experiment_detail(experiment_id):
    """Detailed experiment status + evaluation results."""
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    conn = db.get_connection()
    try:
        status = _exp.get_experiment_status(conn, experiment_id)
        if "error" in status:
            return jsonify({"error": status["error"]}), 404
        evaluation = _eval.evaluate_experiment(conn, experiment_id)
    finally:
        conn.close()
    return jsonify({"status": status, "evaluation": evaluation})


@app.route("/api/learning/experiments/complete", methods=["POST"])
def api_learning_experiment_complete():
    """Mark an experiment completed and record all remaining terminal outcomes.

    Body: { "experiment_id": "..." }
    Protected by X-API-Key.
    """
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    payload = request.get_json(silent=True) or {}
    experiment_id = (payload.get("experiment_id") or "").strip()
    if not experiment_id:
        return jsonify({"ok": False, "message": "experiment_id is required."}), 400

    conn = db.get_connection()
    try:
        result = _exp.complete_experiment(conn, experiment_id)
    finally:
        conn.close()
    if not result.get("completed"):
        return jsonify({"ok": False, **result}), 400
    return jsonify({"ok": True, **result})


# --- Policy Recommendations ---

@app.route("/api/learning/recommendations", methods=["GET"])
def api_learning_recommendations_list():
    """List policy recommendations. Optionally filter by status."""
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    status_filter = request.args.get("status")
    merchant_cat = request.args.get("merchant_category")
    conn = db.get_connection()
    try:
        recs = db.get_all_recommendations(
            conn,
            status=status_filter or None,
            merchant_category=merchant_cat or None,
        )
        import json as _json
        for r in recs:
            try:
                r["why_evidence_parsed"] = _json.loads(r.get("why_evidence") or "{}")
            except Exception:
                r["why_evidence_parsed"] = {}
    finally:
        conn.close()
    return jsonify({"recommendations": recs, "count": len(recs)})


@app.route("/api/learning/recommendations/generate", methods=["POST"])
def api_learning_recommendations_generate():
    """Scan strategy_performance and generate new recommendations.

    Only creates recommendations where evidence meets the minimum threshold.
    Does not overwrite existing active recommendations.
    Protected by X-API-Key.
    """
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    conn = db.get_connection()
    try:
        new_recs = _policy.generate_recommendations(conn)
    finally:
        conn.close()
    return jsonify({
        "ok": True,
        "new_recommendations": len(new_recs),
        "recommendations": new_recs,
    })


@app.route("/api/learning/recommendations/<recommendation_id>")
def api_learning_recommendation_detail(recommendation_id):
    """Full recommendation detail including parsed evidence trail."""
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    conn = db.get_connection()
    try:
        rec = db.get_recommendation(conn, recommendation_id)
        if not rec:
            return jsonify({"error": "recommendation not found"}), 404
        import json as _json
        try:
            rec["why_evidence_parsed"] = _json.loads(rec.get("why_evidence") or "{}")
        except Exception:
            rec["why_evidence_parsed"] = {}
        # Include audit trail for this recommendation
        audit = db.get_policy_audit_log(conn, recommendation_id=recommendation_id)
    finally:
        conn.close()
    return jsonify({"recommendation": rec, "audit_trail": audit})


@app.route("/api/learning/recommendations/approve", methods=["POST"])
def api_learning_recommendation_approve():
    """Approve a recommendation and activate the resulting policy version.

    Body: { "recommendation_id": "...", "actor": "..." }
    Creates a new DRAFT policy version from the recommendation, transitions it
    through recommended → under_review → approved → active in one step.
    Protected by X-API-Key.
    """
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    payload = request.get_json(silent=True) or {}
    rec_id = (payload.get("recommendation_id") or "").strip()
    actor = (payload.get("actor") or "dashboard_user").strip()
    if not rec_id:
        return jsonify({"ok": False, "message": "recommendation_id is required."}), 400

    conn = db.get_connection()
    try:
        result = _policy.approve_recommendation(conn, rec_id, actor=actor)
    finally:
        conn.close()
    if not result.get("approved"):
        return jsonify({"ok": False, **result}), 400
    return jsonify({"ok": True, **result})


@app.route("/api/learning/recommendations/reject", methods=["POST"])
def api_learning_recommendation_reject():
    """Reject a recommendation with a reason.

    Body: { "recommendation_id": "...", "actor": "...", "reason": "..." }
    Protected by X-API-Key.
    """
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    payload = request.get_json(silent=True) or {}
    rec_id = (payload.get("recommendation_id") or "").strip()
    actor = (payload.get("actor") or "dashboard_user").strip()
    reason = (payload.get("reason") or "").strip()
    if not rec_id:
        return jsonify({"ok": False, "message": "recommendation_id is required."}), 400
    if not reason:
        return jsonify({"ok": False, "message": "reason is required for rejection."}), 400

    conn = db.get_connection()
    try:
        ok = _policy.reject_recommendation(conn, rec_id, actor=actor, reason=reason)
    finally:
        conn.close()
    return jsonify({"ok": ok, "recommendation_id": rec_id})


# --- Policy Versions ---

@app.route("/api/learning/policy/active")
def api_learning_policy_active():
    """Return the currently active policy version (or rule-based defaults)."""
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    merchant_cat = request.args.get("merchant_category", "all")
    conn = db.get_connection()
    try:
        result = _policy.get_active_policy(conn, merchant_cat)
    finally:
        conn.close()
    return jsonify(result)


@app.route("/api/learning/policy/history")
def api_learning_policy_history():
    """Full policy version history for a merchant category."""
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    merchant_cat = request.args.get("merchant_category", "all")
    conn = db.get_connection()
    try:
        history = _policy.get_policy_history(conn, merchant_cat)
    finally:
        conn.close()
    return jsonify({"history": history, "count": len(history)})


@app.route("/api/learning/policy/<version_id>")
def api_learning_policy_version_detail(version_id):
    """Full detail for one policy version including performance records and audit."""
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    conn = db.get_connection()
    try:
        version = db.get_policy_version(conn, version_id)
        if not version:
            return jsonify({"error": "policy version not found"}), 404
        perf = db.get_policy_performance(conn, version_id)
        audit = db.get_policy_audit_log(conn, version_id=version_id)
        import json as _json
        version["strategy_params"] = _policy._parse_json_field(version.get("strategy_params"))
        version["evidence_summary"] = _policy._parse_json_field(version.get("evidence_summary"))
        version["expected_impact"] = _policy._parse_json_field(version.get("expected_impact"))
    finally:
        conn.close()
    return jsonify({
        "version": version,
        "performance": perf,
        "audit_trail": audit,
    })


@app.route("/api/learning/policy/measure", methods=["POST"])
def api_learning_policy_measure():
    """Record a current-performance snapshot for the active policy version.

    Protected by X-API-Key.
    """
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    conn = db.get_connection()
    try:
        result = _policy.record_current_policy_performance(conn)
    finally:
        conn.close()
    return jsonify({"ok": True, **result})


@app.route("/api/learning/policy/rollback", methods=["POST"])
def api_learning_policy_rollback():
    """Roll back to a specific previous policy version.

    Body: { "target_version_id": "...", "actor": "...", "reason": "..." }
    - Deprecates the currently active version
    - Reactivates the target version
    - Creates audit events for both changes
    - Historical performance records are preserved, NOT modified
    Protected by X-API-Key.
    """
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    payload = request.get_json(silent=True) or {}
    target_id = (payload.get("target_version_id") or "").strip()
    actor = (payload.get("actor") or "dashboard_user").strip()
    reason = (payload.get("reason") or "").strip()
    if not target_id:
        return jsonify({"ok": False, "message": "target_version_id is required."}), 400
    if not reason:
        return jsonify({"ok": False, "message": "reason is required for rollback."}), 400

    conn = db.get_connection()
    try:
        result = _policy.rollback_to_version(conn, target_id, actor=actor, reason=reason)
    finally:
        conn.close()
    if not result.get("rolled_back"):
        return jsonify({"ok": False, **result}), 400
    return jsonify({"ok": True, **result})


@app.route("/api/learning/policy/audit-log")
def api_learning_policy_audit_log():
    """Full policy governance audit log — every approval, activation, rollback."""
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    version_id = request.args.get("version_id")
    rec_id = request.args.get("recommendation_id")
    try:
        limit = min(int(request.args.get("limit", 100)), 500)
    except (TypeError, ValueError):
        limit = 100
    conn = db.get_connection()
    try:
        entries = db.get_policy_audit_log(
            conn,
            version_id=version_id or None,
            recommendation_id=rec_id or None,
            limit=limit,
        )
    finally:
        conn.close()
    return jsonify({"audit_log": entries, "count": len(entries)})


# --- Learning Dashboard Summary ---

@app.route("/api/learning/dashboard")
def api_learning_dashboard():
    """Single-call payload for the Learning Dashboard.

    Returns:
      - active_policy: current version or rule-based defaults
      - performance_vs_previous: before/after comparison
      - open_recommendations: pending review items
      - recent_experiments: last 5 experiments with evaluation
      - strategy_learning: provenance summary
      - attribution_summary: coverage stats
      - policy_history_recent: last 5 versions
      - strategy_drift: current drift alerts
    """
    if not _P6_AVAILABLE:
        return _p6_unavailable()
    conn = db.get_connection()
    try:
        summary = _policy.learning_dashboard_summary(conn)
        drift = _drift.detect_strategy_drift(conn)
        summary["strategy_drift"] = drift
    finally:
        conn.close()
    return jsonify(summary)


if __name__ == "__main__":
    db.init_db()
    # Phase 4: reset any stale claimed jobs before serving (handles process restart).
    try:
        scheduler_module.reset_stale_claimed_jobs()
    except Exception:
        pass  # non-fatal on first boot before schema migration runs
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
