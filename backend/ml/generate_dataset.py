"""STEP 1 — Build a labeled training dataset from the REAL simulation.

This script does NOT touch the existing rule-based scoring, agent, or compliance
logic, and it never modifies the live mandate_rescue.db. It re-runs the exact same
seed generator (seed.build_records) and the exact same recovery pipeline
(agent.run_agent) that drive the real product, but against a throwaway temporary
SQLite database, and records the outcome the simulation actually produced.

For each processed case it captures the seven requested features:
    past_payment_success_rate, customer_tenure_months, past_retry_count,
    failure_reason, amount, mandate_limit, merchant_category
and the actual outcome label:
    recovered = 1  (case_status == 'recovered')
    not recovered = 0  (any other terminal status)

180 cases from a single run is too small for a trustworthy stratified train/test
split, so we BOOTSTRAP a larger dataset by repeating the simulation N times with
different random seeds (default 10 runs x 180 = 1800 labeled rows). Each run uses a
distinct seed for BOTH the seed generator and the agent RNG, so runs differ from one
another while each individual run stays fully reproducible.

IMPORTANT / HONESTY NOTE (also written into the CSV header comment and README):
these 1800 rows are bootstrapped from repeated runs of the same 180-case synthetic
simulation. They are NOT 1800 unique real customers. The dataset is a realistic,
label-consistent training set derived from the simulation's own decisions, used to
train and validate a model as an additive research/validation layer.

Rows from spoofed webhooks (rejected at ingestion, so never processed and never
given a recovery outcome) are excluded from training, since they have no genuine
recovered/not-recovered label.
"""

import csv
import os
import random
import sqlite3
import sys
import tempfile

# Make the sibling backend modules importable whether run as a script or a module.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import db
import seed as seed_module
import agent as agent_module

_ML_DIR = os.path.dirname(os.path.abspath(__file__))
TRAINING_CSV = os.path.join(_ML_DIR, "training_data.csv")

# The seven features requested, plus the label column.
FEATURE_COLUMNS = [
    "past_payment_success_rate",
    "customer_tenure_months",
    "past_retry_count",
    "failure_reason",
    "amount",
    "mandate_limit",
    "merchant_category",
]
LABEL_COLUMN = "recovered"

# Bootstrap config: N independent seeded runs of the 180-case simulation.
DEFAULT_RUNS = 10
BASE_SEED = 1000  # run i uses seed BASE_SEED + i (deterministic, reproducible)


def _seed_temp_db(conn, run_seed):
    """Seed `conn`'s database with a fresh 180-case simulation using `run_seed`.

    Reuses seed_module.build_records so the feature distributions are byte-for-byte
    the same generator the product uses; only the RNG seed varies per run.
    """
    rng = random.Random(run_seed)
    records = seed_module.build_records(rng)
    db.reset_db(conn)
    for record in records:
        db.insert_mandate_failure(conn, record)
    conn.commit()
    return records


def _run_one_simulation(run_seed):
    """Seed + run the real agent once against a temp DB; return labeled feature rows.

    Overrides db.DB_PATH and agent.RUN_SEED for the duration of this run, then always
    restores them, so neither the live DB nor the product's default behavior is
    affected. Uses the identical run_agent() pipeline that drives real decisions.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="mr_ml_")
    os.close(fd)

    saved_db_path = db.DB_PATH
    saved_run_seed = agent_module.RUN_SEED
    rows = []
    try:
        # Point every db.get_connection() at the throwaway database.
        db.DB_PATH = tmp_path
        # Vary the agent's RNG per run so bootstrapped runs differ from each other,
        # while remaining reproducible for a given run_seed.
        agent_module.RUN_SEED = run_seed

        conn = db.get_connection()
        try:
            db.init_db(conn)
            _seed_temp_db(conn, run_seed)
        finally:
            conn.close()

        # Run the exact same recovery pipeline the product uses. This writes final
        # case_status values into the temp DB.
        agent_module.run_agent()

        conn = db.get_connection()
        try:
            for case in db.get_all_cases(conn):
                # Skip spoofed/rejected events: they never entered the pipeline and
                # thus carry no genuine recovered/not-recovered outcome.
                if case.get("case_status") == "rejected":
                    continue
                rows.append({
                    "past_payment_success_rate": case["past_payment_success_rate"],
                    "customer_tenure_months": case["customer_tenure_months"],
                    "past_retry_count": case["past_retry_count"],
                    "failure_reason": case["failure_reason"],
                    "amount": round(float(case["amount"]), 2),
                    "mandate_limit": round(float(case.get("mandate_limit") or 5000), 2),
                    "merchant_category": case["merchant_category"],
                    LABEL_COLUMN: 1 if case["case_status"] == "recovered" else 0,
                })
        finally:
            conn.close()
    finally:
        # Restore product defaults no matter what.
        db.DB_PATH = saved_db_path
        agent_module.RUN_SEED = saved_run_seed
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return rows


def generate(runs=DEFAULT_RUNS, out_path=TRAINING_CSV):
    """Generate the bootstrapped labeled dataset and write it to `out_path` as CSV.

    Returns (row_count, positive_count). Fully deterministic for a given `runs`.
    """
    all_rows = []
    for i in range(runs):
        run_seed = BASE_SEED + i
        run_rows = _run_one_simulation(run_seed)
        all_rows.extend(run_rows)
        pos = sum(r[LABEL_COLUMN] for r in run_rows)
        print(f"  run {i + 1}/{runs} (seed={run_seed}): "
              f"{len(run_rows)} rows, {pos} recovered")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    header = FEATURE_COLUMNS + [LABEL_COLUMN]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        # Provenance comment so the dataset is honest about how it was built.
        f.write(
            "# Mandate Rescue training data. BOOTSTRAPPED from "
            f"{runs} repeated runs of the 180-case synthetic simulation with "
            "distinct seeds. These rows are NOT unique real customers; they are "
            "label-consistent outcomes produced by the product's own rule-based "
            "recovery pipeline. Used to train/validate an additive ML layer that "
            "does NOT drive agent decisions.\n"
        )
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        for row in all_rows:
            writer.writerow(row)

    positives = sum(r[LABEL_COLUMN] for r in all_rows)
    return len(all_rows), positives


if __name__ == "__main__":
    n_runs = DEFAULT_RUNS
    if len(sys.argv) > 1:
        try:
            n_runs = int(sys.argv[1])
        except ValueError:
            pass
    print(f"Generating bootstrapped training data from {n_runs} simulation runs\u2026")
    total, positives = generate(runs=n_runs)
    rate = (positives / total) if total else 0.0
    print(f"\nWrote {total} labeled rows to {TRAINING_CSV}")
    print(f"Recovered (label=1): {positives} ({rate:.1%})  |  "
          f"Not recovered (label=0): {total - positives} ({1 - rate:.1%})")
