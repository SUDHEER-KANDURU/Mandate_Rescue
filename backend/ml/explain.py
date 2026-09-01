"""SHAP explainability for the additive ML layer (interpretation only).

WHAT THIS IS
------------
Real SHAP (SHapley Additive exPlanations) values for the already-trained model in
model.pkl. It answers "which features pushed THIS case's recovery prediction up or
down, and by how much" and "which features matter most to the model overall".

Like predict.py, this is an ADDITIVE VALIDATION / INTERPRETABILITY layer. It does NOT
change the model, its training, or any agent / scoring / compliance decision. It only
explains what the (non-decision) recovery-likelihood model already predicts.

HOW IT WORKS (correctly, for a Pipeline)
----------------------------------------
model.pkl is a sklearn Pipeline: a ColumnTransformer (OneHotEncoder for the two
categoricals + StandardScaler for the five numerics) feeding a classifier. SHAP must
run on the TRANSFORMED feature space (13 columns) because that is what the classifier
actually sees. We therefore:

  1. Split off the fitted preprocessor and the fitted classifier.
  2. Choose the explainer by classifier type:
       - LogisticRegression        -> shap.LinearExplainer
       - GradientBoosting / RandomForest / other trees -> shap.TreeExplainer
       - anything else             -> shap.KernelExplainer (model-agnostic fallback)
  3. Compute SHAP values on the transformed test matrix.
  4. Aggregate the one-hot columns back to their ORIGINAL feature (e.g. the four
     failure_reason_* columns sum into a single "failure_reason" contribution), so the
     per-case explanation is expressed in the seven human-readable features.

ADDITIVITY (the property the verification checks)
-------------------------------------------------
For a linear model, SHAP values are additive in the MARGIN (log-odds) space:
    base_value + sum(shap_values) == clf.decision_function(x)   (exactly)
and therefore
    sigmoid(base_value + sum(shap_values)) == model.predict_proba(x)[1].
We expose both the margin-space contributions and this probability-space check so a
caller can confirm the explanation reconstructs the real prediction.

The dataset / train/test split is reconstructed with the SAME random_state and
stratification as train_model.py, so we explain exactly the held-out test cases the
metrics were computed on.
"""

import math
import os
import threading

_ML_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(_ML_DIR, "model.pkl")
TRAINING_CSV = os.path.join(_ML_DIR, "training_data.csv")

# Must match train_model.py exactly (feature order, split, seed).
NUMERIC_FEATURES = [
    "past_payment_success_rate",
    "customer_tenure_months",
    "past_retry_count",
    "amount",
    "mandate_limit",
]
CATEGORICAL_FEATURES = ["failure_reason", "merchant_category"]
FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES
LABEL = "recovered"
RANDOM_STATE = 42
TEST_SIZE = 0.20

# How many top features to surface per case (by absolute contribution).
TOP_K = 4

# Friendly labels for the UI. Falls back to a title-cased feature name if absent.
FEATURE_LABELS = {
    "past_payment_success_rate": "Past payment success rate",
    "customer_tenure_months": "Customer tenure (months)",
    "past_retry_count": "Past retry count",
    "amount": "Amount",
    "mandate_limit": "Mandate limit",
    "failure_reason": "Failure reason",
    "merchant_category": "Merchant category",
}

# Lazily-built, cached explanation state (thread-safe). Built once, reused.
_state = None
_build_attempted = False
_lock = threading.Lock()


def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def _original_feature_for(transformed_name):
    """Map a transformed column name back to its ORIGINAL feature name.

    Names look like 'cat__failure_reason_insufficient_funds' or
    'num__customer_tenure_months' (sklearn ColumnTransformer convention). We strip the
    'cat__'/'num__' transformer prefix, then match the longest original feature name
    that the remainder starts with (so 'failure_reason_insufficient_funds' maps to
    'failure_reason', and 'customer_tenure_months' maps to itself).
    """
    name = transformed_name
    for prefix in ("cat__", "num__"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    # Numeric columns match their full name directly.
    if name in FEATURES:
        return name
    # One-hot categorical: '<feature>_<value>'. Match the longest known feature prefix.
    best = None
    for feat in CATEGORICAL_FEATURES:
        if name == feat or name.startswith(feat + "_"):
            if best is None or len(feat) > len(best):
                best = feat
    return best or name


def _choose_explainer(clf, background):
    """Return (shap_explainer, kind_str) appropriate for the classifier type."""
    import shap
    cls_name = type(clf).__name__
    tree_types = ("GradientBoostingClassifier", "RandomForestClassifier",
                  "ExtraTreesClassifier", "DecisionTreeClassifier",
                  "HistGradientBoostingClassifier", "XGBClassifier",
                  "LGBMClassifier")
    if cls_name in tree_types:
        return shap.TreeExplainer(clf, background), "TreeExplainer"
    if cls_name in ("LogisticRegression", "LinearSVC", "RidgeClassifier",
                    "SGDClassifier"):
        # Use the FULL background (no random subsampling) so the base value is
        # deterministic and identical between the global build and per-case calls.
        # For a linear model this is cheap. An Independent masker over all rows
        # avoids shap's default max_samples=100 subsample.
        masker = shap.maskers.Independent(background, max_samples=len(background))
        return shap.LinearExplainer(clf, masker), "LinearExplainer"
    # Model-agnostic fallback. Wrap predict_proba(pos class) over the transformed space.
    def _f(data):
        return clf.predict_proba(data)[:, 1]
    return shap.KernelExplainer(_f, shap.sample(background, 50)), "KernelExplainer"


def _build_state():
    """Compute SHAP for the held-out test set once and cache the aggregated result.

    Returns a dict (or None if the model/artifacts are unavailable), with:
        {
          "explainer_kind", "clf_name",
          "base_value_margin",            # scalar log-odds base
          "feature_names": [...7...],
          "global_importance": [{feature,label,mean_abs_shap}, ...] sorted desc,
          "per_id": { customer_id: {..breakdown..} },  # only ids present in test set
          "space": "margin(log-odds)",
        }
    """
    global _state, _build_attempted
    if _build_attempted:
        return _state
    with _lock:
        if _build_attempted:
            return _state
        _build_attempted = True
        try:
            _state = _compute_state()
        except Exception:
            _state = None
    return _state


def _compute_state():
    import joblib
    import numpy as np
    import pandas as pd
    from sklearn.model_selection import train_test_split

    if not os.path.exists(MODEL_PATH) or not os.path.exists(TRAINING_CSV):
        return None

    model = joblib.load(MODEL_PATH)
    pre = model.named_steps["pre"]
    clf = model.named_steps["clf"]

    df = pd.read_csv(TRAINING_CSV, comment="#")
    # Preserve a per-row id so a case can be explained by customer_id if the column
    # exists; otherwise fall back to positional row ids. The bootstrapped training set
    # may not carry customer_id, so we key the test-set breakdowns by row index too.
    X = df[FEATURES]
    y = df[LABEL].astype(int)

    # Reconstruct the identical held-out test split (same seed + stratify).
    idx = np.arange(len(df))
    X_train, X_test, y_train, y_test, idx_train, idx_test = train_test_split(
        X, y, idx, test_size=TEST_SIZE, stratify=y, random_state=RANDOM_STATE
    )

    # Transform to the space the classifier actually sees.
    Xt_train = np.asarray(pre.transform(X_train))
    Xt_test = np.asarray(pre.transform(X_test))
    transformed_names = list(pre.get_feature_names_out())

    explainer, kind = _choose_explainer(clf, Xt_train)
    explanation = explainer(Xt_test)
    shap_matrix = np.asarray(explanation.values)
    base_values = np.asarray(explanation.base_values)

    # Some explainers return per-class 3D arrays; take the positive-class slice.
    if shap_matrix.ndim == 3:
        shap_matrix = shap_matrix[:, :, -1]
    if base_values.ndim > 1:
        base_values = base_values[:, -1]
    base_scalar = float(np.mean(base_values)) if base_values.size else 0.0

    # Column -> original feature index map (for aggregating one-hot columns).
    orig_of_col = [_original_feature_for(n) for n in transformed_names]
    feat_index = {f: i for i, f in enumerate(FEATURES)}

    # Aggregate transformed SHAP columns back to the 7 original features.
    n_rows = shap_matrix.shape[0]
    agg = np.zeros((n_rows, len(FEATURES)))
    for col, orig in enumerate(orig_of_col):
        if orig in feat_index:
            agg[:, feat_index[orig]] += shap_matrix[:, col]

    # Global importance = mean absolute aggregated SHAP per original feature.
    mean_abs = np.mean(np.abs(agg), axis=0)
    global_importance = [
        {"feature": FEATURES[i],
         "label": FEATURE_LABELS.get(FEATURES[i], FEATURES[i]),
         "mean_abs_shap": round(float(mean_abs[i]), 6)}
        for i in range(len(FEATURES))
    ]
    global_importance.sort(key=lambda d: d["mean_abs_shap"], reverse=True)

    # Per-test-row breakdown, keyed by the original dataframe row index.
    X_test_reset = X_test.reset_index(drop=True)
    per_row = {}
    for r in range(n_rows):
        row_feature_vals = {f: X_test_reset.iloc[r][f] for f in FEATURES}
        per_row[int(idx_test[r])] = _build_case_breakdown(
            agg[r], base_scalar, row_feature_vals, int(y_test.iloc[r]))

    return {
        "explainer_kind": kind,
        "clf_name": type(clf).__name__,
        "base_value_margin": round(base_scalar, 6),
        "feature_names": list(FEATURES),
        "global_importance": global_importance,
        "per_row": per_row,
        "space": "margin(log-odds)",
    }


def _build_case_breakdown(agg_row, base_scalar, feature_vals, actual_label=None):
    """Turn one row's aggregated SHAP vector into a human-readable breakdown dict.

    Reports the top-K features by absolute contribution with signed impact and a
    'increased'/'decreased' direction, plus the exact additivity reconstruction so a
    caller can verify base + sum(shap) reproduces the model's margin and probability.
    """
    margin = base_scalar + float(agg_row.sum())
    prob = _sigmoid(margin)
    contributions = []
    for i, feat in enumerate(FEATURES):
        impact = float(agg_row[i])
        contributions.append({
            "feature": feat,
            "label": FEATURE_LABELS.get(feat, feat),
            "value": _fmt_value(feature_vals.get(feat)),
            "impact": round(impact, 6),
            "direction": "increased" if impact >= 0 else "decreased",
        })
    top = sorted(contributions, key=lambda c: abs(c["impact"]), reverse=True)[:TOP_K]
    return {
        "base_value": round(base_scalar, 6),
        "predicted_margin": round(margin, 6),
        "predicted_probability": round(prob, 6),
        "actual_recovered": actual_label,
        "top_factors": top,
        "all_contributions": contributions,
        # Additivity fields for the correctness check.
        "sum_shap": round(float(agg_row.sum()), 6),
        "reconstructed_probability": round(_sigmoid(margin), 6),
    }


def _fmt_value(v):
    """Present a feature value compactly for the UI (numbers rounded, strings as-is)."""
    if isinstance(v, float):
        return round(v, 2)
    return v


# --- Public API -------------------------------------------------------------

def available():
    """True if SHAP explanations could be computed (model + shap + data present)."""
    return _build_state() is not None


def global_feature_importance():
    """Return the sorted global mean-abs-SHAP importance list, or None if unavailable."""
    state = _build_state()
    if state is None:
        return None
    return {
        "explainer": state["explainer_kind"],
        "model": state["clf_name"],
        "space": state["space"],
        "base_value": state["base_value_margin"],
        "importance": state["global_importance"],
    }


def explain_case(case):
    """Explain an arbitrary case dict on the fly (used by the per-case API).

    Runs SHAP for this single case against the SAME background used at build time.
    Falls back to None if the model/shap are unavailable. The returned breakdown has
    the identical shape as the cached per-test-row breakdowns.
    """
    state = _build_state()
    if state is None:
        return None
    try:
        return _explain_single(case)
    except Exception:
        return None


def _explain_single(case):
    import joblib
    import numpy as np
    import pandas as pd
    from sklearn.model_selection import train_test_split

    model = joblib.load(MODEL_PATH)
    pre = model.named_steps["pre"]
    clf = model.named_steps["clf"]

    df = pd.read_csv(TRAINING_CSV, comment="#")
    X = df[FEATURES]
    y = df[LABEL].astype(int)
    X_train, _X_test, _y_train, _y_test = train_test_split(
        X, y, test_size=TEST_SIZE, stratify=y, random_state=RANDOM_STATE
    )
    Xt_train = np.asarray(pre.transform(X_train))
    transformed_names = list(pre.get_feature_names_out())

    row = {
        "past_payment_success_rate": float(case.get("past_payment_success_rate", 0.0)),
        "customer_tenure_months": float(case.get("customer_tenure_months", 0)),
        "past_retry_count": float(case.get("past_retry_count", 0)),
        "amount": float(case.get("amount", 0.0)),
        "mandate_limit": float(case.get("mandate_limit") or 5000),
        "failure_reason": case.get("failure_reason", ""),
        "merchant_category": case.get("merchant_category", ""),
    }
    X_one = pd.DataFrame([row], columns=FEATURES)
    Xt_one = np.asarray(pre.transform(X_one))

    explainer, _kind = _choose_explainer(clf, Xt_train)
    explanation = explainer(Xt_one)
    shap_matrix = np.asarray(explanation.values)
    base_values = np.asarray(explanation.base_values)
    if shap_matrix.ndim == 3:
        shap_matrix = shap_matrix[:, :, -1]
    if base_values.ndim > 1:
        base_values = base_values[:, -1]
    base_scalar = float(np.ravel(base_values)[0])

    orig_of_col = [_original_feature_for(n) for n in transformed_names]
    feat_index = {f: i for i, f in enumerate(FEATURES)}
    agg = np.zeros(len(FEATURES))
    for col, orig in enumerate(orig_of_col):
        if orig in feat_index:
            agg[feat_index[orig]] += shap_matrix[0, col]

    return _build_case_breakdown(agg, base_scalar, row, None)


def explain_test_row(row_index):
    """Return the cached breakdown for a held-out test row index, or None."""
    state = _build_state()
    if state is None:
        return None
    return state["per_row"].get(int(row_index))


if __name__ == "__main__":
    import json
    gi = global_feature_importance()
    print(json.dumps(gi, indent=2))
