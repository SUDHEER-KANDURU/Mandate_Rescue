"""Inference helper for the additive ML layer.

Loads the trained model.pkl (produced by train_model.py) once and exposes a
recovery-probability prediction for a case. This is a TRANSPARENT, NON-DECISION
layer: the value it returns is displayed alongside the rule-based recoverability
score for comparison, and is never consulted by the agent, scoring, or compliance
logic. If the model artifact is missing (training not yet run), every helper degrades
gracefully to "unavailable" so the app keeps working exactly as before.
"""

import json
import os
import threading

_ML_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(_ML_DIR, "model.pkl")
METRICS_PATH = os.path.join(_ML_DIR, "metrics.json")

# Feature order/names must match what train_model.py fit on.
NUMERIC_FEATURES = [
    "past_payment_success_rate",
    "customer_tenure_months",
    "past_retry_count",
    "amount",
    "mandate_limit",
]
CATEGORICAL_FEATURES = ["failure_reason", "merchant_category"]
FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES

_model = None
_load_attempted = False
_lock = threading.Lock()


def _load_model():
    """Lazily load model.pkl once (thread-safe). Returns the pipeline or None."""
    global _model, _load_attempted
    if _load_attempted:
        return _model
    with _lock:
        if _load_attempted:
            return _model
        _load_attempted = True
        try:
            import joblib  # imported lazily so the app runs even without sklearn
            if os.path.exists(MODEL_PATH):
                _model = joblib.load(MODEL_PATH)
        except Exception:
            _model = None
    return _model


def model_available():
    """True if a trained model artifact is loaded and ready for inference."""
    return _load_model() is not None


def load_metrics():
    """Return the saved metrics.json dict, or None if it does not exist yet.

    This is the single source of truth for the /api/ml-metrics endpoint and the
    dashboard panel; no evaluation number is hardcoded elsewhere.
    """
    if not os.path.exists(METRICS_PATH):
        return None
    try:
        with open(METRICS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def predict_recovery_probability(case):
    """Return P(recovered) in [0,1] for a case dict, or None if model unavailable.

    Accepts a raw mandate_failures-style dict; the saved pipeline handles one-hot
    encoding + scaling internally. Purely informational — callers must not use this
    to change any retry/escalation/compliance behavior.
    """
    model = _load_model()
    if model is None:
        return None
    try:
        import pandas as pd
        row = {
            "past_payment_success_rate": float(case.get("past_payment_success_rate", 0.0)),
            "customer_tenure_months": float(case.get("customer_tenure_months", 0)),
            "past_retry_count": float(case.get("past_retry_count", 0)),
            "amount": float(case.get("amount", 0.0)),
            "mandate_limit": float(case.get("mandate_limit") or 5000),
            "failure_reason": case.get("failure_reason", ""),
            "merchant_category": case.get("merchant_category", ""),
        }
        X = pd.DataFrame([row], columns=FEATURES)
        proba = model.predict_proba(X)[0, 1]
        return round(float(proba), 4)
    except Exception:
        return None
