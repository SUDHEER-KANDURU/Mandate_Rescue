# ML Dependency Version Strategy

## Problem Statement (Stage 0 Finding)

Before Stage 0, `requirements.txt` pinned these versions:
```
scikit-learn==1.5.2
joblib==1.4.2
numpy==1.26.4
```

But the actual runtime environment had:
```
scikit-learn==1.9.0
joblib==1.5.3   (later updated to 1.6.0)
numpy==2.5.2
```

The `model.pkl` artifact was serialized with sklearn 1.9.0 + joblib 1.5.3, producing this warning on every model load:

```
DeprecationWarning: Setting the shape on a NumPy array has been deprecated in NumPy 2.5.
As an alternative, you can create a new view using np.reshape
  array.shape = self.shape   (joblib/numpy_pickle.py:207)
```

This warning repeated 7 times per test run and would appear on every server startup model warm-up, damaging credibility during live demos.

---

## Root Cause Analysis

| Component | Pinned | Actual | Problem |
|---|---|---|---|
| `scikit-learn` | 1.5.2 | 1.9.0 | requirements.txt was out of date; model was trained on 1.9.0 |
| `joblib` | 1.4.2 | 1.5.3/1.6.0 | joblib 1.5.3's `numpy_pickle.py` uses `array.shape = shape` which is deprecated in NumPy 2.5 |
| `numpy` | 1.26.4 | 2.5.2 | NumPy 2.5 deprecated setting `.shape` attribute directly; this triggers the joblib warning |

The warning was **not a model compatibility issue** — the model loaded and produced correct predictions. It was a **joblib serialization format issue** where the pickled file stored arrays in a format that triggers the NumPy 2.5 deprecation on deserialization.

---

## Fix Applied

### Step 1 — Upgrade joblib to 1.6.0

joblib 1.6.0 updates `numpy_pickle.py` to use `np.reshape()` instead of `array.shape = shape`, eliminating the NumPy 2.5 DeprecationWarning.

```bash
python -m pip install joblib==1.6.0
```

### Step 2 — Retrain model.pkl under the correct runtime

Re-ran `python backend/ml/train_model.py` to serialize the model artifact with joblib 1.6.0 + NumPy 2.5.2. This ensures the pickle format stored on disk matches the deserialization path exactly.

### Step 3 — Update requirements.txt to match actual runtime

`requirements.txt` now reflects the versions actually installed:
```
scikit-learn==1.9.0
joblib==1.6.0
pandas==2.2.3
numpy==2.5.2
```

### Step 4 — Update Dockerfile base image from python:3.11-slim to python:3.12-slim

Aligns the Docker build with the Python 3.12 runtime used locally, ensuring consistent behavior between development and containerized deployment.

---

## Verification

```bash
# Verify model loads without any DeprecationWarning
python -W error::DeprecationWarning -c "
import joblib
model = joblib.load('backend/ml/model.pkl')
print('Model loaded cleanly — zero DeprecationWarnings')
"
# Output: Model loaded cleanly — zero DeprecationWarnings
```

```bash
# Full test suite — warnings section
pytest --tb=short -q
# 654 passed, 2 skipped, 2 deselected, 0 warnings
```

---

## Model Metrics (unchanged — same training data, same algorithm)

| Model | Precision | Recall | F1 | ROC-AUC |
|---|---|---|---|---|
| LogisticRegression (winner) | 0.9013 | 1.0000 | 0.9481 | 0.8964 |
| GradientBoostingClassifier | 0.9010 | 0.9964 | 0.9463 | 0.8856 |

Model behavior is **identical** — the retraining used the same `training_data.csv`, `RANDOM_STATE=42`, and `TEST_SIZE=0.20`.

---

## Honest Limitations (unchanged)

The ML model is an **additive validation layer only**. It:
- Is trained on 1,770 rows bootstrapped from 180-case synthetic simulation runs
- Uses labels from the rule-based recovery pipeline, not real payment outcomes
- Is never consulted by the agent, scoring, or compliance logic
- Displays its predictions as informational alongside the rule-based score

This limitation is documented in `metrics.json`, `predict.py`, `train_model.py`, and the README.

---

## Docker / CI Alignment

| Component | Before | After |
|---|---|---|
| `requirements.txt` sklearn | 1.5.2 | 1.9.0 |
| `requirements.txt` joblib | 1.4.2 | 1.6.0 |
| `requirements.txt` numpy | 1.26.4 | 2.5.2 |
| Dockerfile base | python:3.11-slim | python:3.12-slim |
| `model.pkl` serialized with | sklearn 1.9.0 + joblib 1.5.3 + numpy 2.5.2 | sklearn 1.9.0 + joblib 1.6.0 + numpy 2.5.2 |
| Warning on model load | 7× DeprecationWarning | ✅ None |
