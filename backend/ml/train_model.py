"""STEP 2 — Train and evaluate a REAL model on the simulation-derived dataset.

This is an ADDITIVE research/validation layer. It trains a genuine scikit-learn model
to predict recovery likelihood from the seven case features, evaluates it honestly on
a held-out test set, and saves the winner. It does NOT touch or influence the
rule-based scoring, the agent pipeline, or the compliance logic — those remain the
sole drivers of the product's actual decisions.

Pipeline:
  1. Load backend/ml/training_data.csv (bootstrapped from repeated simulation runs).
  2. One-hot encode the categorical features (failure_reason, merchant_category);
     pass the numeric features through unchanged.
  3. Stratified 80/20 train/test split with a fixed random_state (reproducible).
  4. Train two models:
        - LogisticRegression        (interpretable baseline)
        - GradientBoostingClassifier (stronger)
  5. Evaluate BOTH on the untouched test set: precision, recall, F1, ROC-AUC, and a
     confusion matrix.
  6. Save the better model (by ROC-AUC) to model.pkl, and write every evaluated
     number to metrics.json (the single source of truth — nothing is hardcoded in
     the app or the dashboard).
  7. Print a clear side-by-side comparison table.

Reproducibility: RANDOM_STATE is fixed and both estimators are seeded, so running
this script twice on the same training_data.csv yields identical metrics.json.
"""

import json
import os

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

_ML_DIR = os.path.dirname(os.path.abspath(__file__))
TRAINING_CSV = os.path.join(_ML_DIR, "training_data.csv")
MODEL_PATH = os.path.join(_ML_DIR, "model.pkl")
METRICS_PATH = os.path.join(_ML_DIR, "metrics.json")

RANDOM_STATE = 42
TEST_SIZE = 0.20

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


def load_dataset(path=TRAINING_CSV):
    """Load the labeled dataset. The first line is a '#' provenance comment."""
    df = pd.read_csv(path, comment="#")
    missing = [c for c in FEATURES + [LABEL] if c not in df.columns]
    if missing:
        raise ValueError(f"training_data.csv is missing columns: {missing}")
    return df


def _build_pipeline(estimator):
    """One-hot encode categoricals, pass numerics through, then the estimator.

    Wrapping the preprocessing + model in one Pipeline means model.pkl accepts a raw
    feature DataFrame at inference time (the app never has to re-implement encoding).
    """
    # Numeric features are on very different scales (e.g. success_rate 0-1 vs amount
    # up to ~15000), so standardize them. This lets LogisticRegression converge and
    # keeps distance-sensitive coefficients meaningful; tree models are unaffected.
    pre = ColumnTransformer(
        transformers=[
            ("cat", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL_FEATURES),
            ("num", StandardScaler(), NUMERIC_FEATURES),
        ]
    )
    return Pipeline([("pre", pre), ("clf", estimator)])


def _evaluate(model, X_test, y_test):
    """Return a metrics dict computed on the held-out test set."""
    y_pred = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]
    cm = confusion_matrix(y_test, y_pred, labels=[0, 1])
    tn, fp, fn, tp = cm.ravel()
    return {
        "precision": round(float(precision_score(y_test, y_pred, zero_division=0)), 4),
        "recall": round(float(recall_score(y_test, y_pred, zero_division=0)), 4),
        "f1": round(float(f1_score(y_test, y_pred, zero_division=0)), 4),
        "roc_auc": round(float(roc_auc_score(y_test, y_proba)), 4),
        "confusion_matrix": {
            "true_negatives": int(tn),
            "false_positives": int(fp),
            "false_negatives": int(fn),
            "true_positives": int(tp),
            "matrix": [[int(tn), int(fp)], [int(fn), int(tp)]],
            "labels": ["not_recovered (0)", "recovered (1)"],
        },
    }


def _print_comparison(results):
    """Print a clean side-by-side metrics table for both models."""
    cols = ["precision", "recall", "f1", "roc_auc"]
    name_w = max(len(name) for name in results) + 2
    header = "Model".ljust(name_w) + "".join(c.upper().rjust(12) for c in cols)
    print("\n" + header)
    print("-" * len(header))
    for name, res in results.items():
        row = name.ljust(name_w) + "".join(f"{res['metrics'][c]:.4f}".rjust(12) for c in cols)
        print(row)
    print()
    for name, res in results.items():
        cm = res["metrics"]["confusion_matrix"]
        print(f"{name} confusion matrix (rows=actual, cols=predicted):")
        print(f"                 pred:0   pred:1")
        print(f"   actual 0 |   {cm['true_negatives']:6d}   {cm['false_positives']:6d}")
        print(f"   actual 1 |   {cm['false_negatives']:6d}   {cm['true_positives']:6d}")
        print()


def train(dataset_path=TRAINING_CSV, model_path=MODEL_PATH, metrics_path=METRICS_PATH):
    """Train + evaluate both models, save the winner + metrics.json. Returns metrics."""
    df = load_dataset(dataset_path)
    X = df[FEATURES]
    y = df[LABEL].astype(int)

    # Stratified split so both classes keep their proportion in train and test.
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=TEST_SIZE, stratify=y, random_state=RANDOM_STATE
    )

    candidates = {
        "LogisticRegression": _build_pipeline(
            LogisticRegression(max_iter=1000, random_state=RANDOM_STATE)
        ),
        "GradientBoostingClassifier": _build_pipeline(
            GradientBoostingClassifier(random_state=RANDOM_STATE)
        ),
    }

    results = {}
    for name, pipe in candidates.items():
        pipe.fit(X_train, y_train)
        results[name] = {"pipeline": pipe, "metrics": _evaluate(pipe, X_test, y_test)}

    _print_comparison(results)

    # Winner = highest ROC-AUC on the held-out test set.
    best_name = max(results, key=lambda n: results[n]["metrics"]["roc_auc"])
    best = results[best_name]
    joblib.dump(best["pipeline"], model_path)

    # metrics.json is the single source of truth for the app/dashboard. Nothing here
    # is hardcoded anywhere else; the UI reads these exact numbers back.
    metrics_out = {
        "best_model": best_name,
        "dataset": {
            "source": ("Bootstrapped from repeated runs of the 180-case synthetic "
                       "simulation with distinct seeds. Not unique real customers; "
                       "label-consistent outcomes from the rule-based recovery "
                       "pipeline. Additive validation layer only \u2014 does not "
                       "drive agent decisions."),
            "total_rows": int(len(df)),
            "positive_rate": round(float(y.mean()), 4),
            "train_size": int(len(X_train)),
            "test_size": int(len(X_test)),
            "test_split_fraction": TEST_SIZE,
            "random_state": RANDOM_STATE,
        },
        "features": {
            "numeric": NUMERIC_FEATURES,
            "categorical": CATEGORICAL_FEATURES,
        },
        "models": {name: results[name]["metrics"] for name in results},
        "winner_metrics": best["metrics"],
    }
    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(metrics_out, f, indent=2)

    print(f"Best model by ROC-AUC: {best_name} "
          f"(AUC={best['metrics']['roc_auc']:.4f})")
    print(f"Saved model  -> {model_path}")
    print(f"Saved metrics -> {metrics_path}")
    return metrics_out


if __name__ == "__main__":
    train()
