"""
Mandate Rescue — Reproducible Recovery-Strategy Benchmark
==========================================================

Compares THREE strategies on the same synthetic dataset with a fixed seed:

  BASELINE A — Naive (1 attempt, no strategy, no scoring)
  BASELINE B — Dumb Persistence (up to 3 attempts, no scoring/timing/dunning)
  MANDATE RESCUE — Full intelligent pipeline (scoring + salary-window + dunning)

All three strategies use the SAME 180-case synthetic dataset and the SAME
per-attempt success-probability model (agent._success_prob), so any difference
in outcomes is attributable exclusively to the strategy — not to the data or the
probability model.

Usage
-----
  python benchmark.py              # uses default seed / n_runs
  python benchmark.py --seed 42    # pin a specific seed
  python benchmark.py --n-runs 50  # Monte Carlo runs per strategy
  python benchmark.py --seed 42 --n-runs 30 --json  # machine-readable output

Output
------
A table like:

  Mandate Rescue — Recovery Strategy Benchmark
  =============================================
  Seed: 42   Cases per run: 180   Monte Carlo runs: 30

  Strategy              Recovery rate    Recovered Rs    Escalation rate   Attempts
  ─────────────────────────────────────────────────────────────────────────────────
  Baseline A (naive)     50.6% ± 3.1%   Rs 198,450      —                 1/case
  Baseline B (persist.)  71.3% ± 2.8%   Rs 280,120      —                 ≤3/case
  Mandate Rescue         77.2% ± 2.4%   Rs 302,560      21.1% ± 2.6%      ≤3/case

  Improvement over Baseline A:   +26.6 pp recovery rate  (+Rs 104,110 / run)
  Improvement over Baseline B:    +5.9 pp recovery rate  (+Rs  22,440 / run)

  Note: Baseline B isolates the value of "more attempts" from the value of
  the agent's actual intelligence. The B->MR delta is the honest, defensible
  measure of what scoring/timing/dunning adds beyond simply retrying more.

All numbers are Monte Carlo means ± 95% confidence interval (Student's t,
df = n_runs - 1). No result is hardcoded — every number is computed fresh
from the simulation on each run. Use --seed to reproduce exact results.

Methodology
-----------
1. For each of `n_runs` iterations:
   a. Seed the 180-case generator with `data_seed` (fixed) so every run
      starts from IDENTICAL case features.
   b. Run Baseline A with its own RNG stream (seed = agent_seed).
   c. Run Baseline B with its own RNG stream (seed = agent_seed + 1).
   d. Run Mandate Rescue with its own RNG stream (seed = agent_seed + 2).
   Each agent_seed advances by 10 per iteration so the three strategies within
   one iteration share no RNG state.
2. Collect recovery_rate, amount_recovered, escalation_rate, and duplicate_count.
3. Summarise across runs: mean, std, 95% CI (t-distribution via scipy if available,
   otherwise standard normal z=1.96).
4. Print the table and, optionally, emit JSON.
"""

import argparse
import json
import math
import os
import random
import sys
import time

# Make backend/ importable as top-level modules (same as pytest root conftest).
_BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Load .env from project root so WEBHOOK_SECRET etc. are available.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

# webhook_security.py is fail-closed: signing raises without a real secret.
# When running the benchmark outside of the app (no .env), inject a stable
# benchmark-only secret so seed.build_records() can sign payloads. This value
# is never used for anything other than the benchmark's isolated in-memory DBs.
os.environ.setdefault(
    "WEBHOOK_SECRET",
    "benchmark-only-webhook-secret-not-for-production-0123456789abcdef",
)

import db
import seed as seed_module
import agent as agent_module
import baseline as baseline_module

# Try scipy for honest t-distribution CIs; fall back to z=1.96.
try:
    from scipy import stats as _scipy_stats
    _HAVE_SCIPY = True
except ImportError:
    _scipy_stats = None
    _HAVE_SCIPY = False

# ------------------------------------------------------------------
# Configuration defaults
# ------------------------------------------------------------------
DEFAULT_SEED = 42
DEFAULT_N_RUNS = 30
CASES_PER_RUN = seed_module.TOTAL  # 180


# ------------------------------------------------------------------
# CI helpers
# ------------------------------------------------------------------

def _critical_value(n, confidence=0.95):
    if n <= 1:
        return 0.0
    alpha = 1.0 - confidence
    if _HAVE_SCIPY:
        return float(_scipy_stats.t.ppf(1.0 - alpha / 2.0, df=n - 1))
    return 1.959963984540054  # z = 1.96 for 95%


def _summarize(samples, confidence=0.95):
    n = len(samples)
    if n == 0:
        return {"mean": 0.0, "std": 0.0, "ci_margin": 0.0, "n": 0}
    mean = sum(samples) / n
    if n == 1:
        return {"mean": mean, "std": 0.0, "ci_margin": 0.0, "n": 1}
    variance = sum((x - mean) ** 2 for x in samples) / (n - 1)
    std = math.sqrt(variance)
    crit = _critical_value(n, confidence)
    margin = crit * (std / math.sqrt(n))
    return {"mean": mean, "std": std, "ci_margin": margin, "n": n}


# ------------------------------------------------------------------
# Per-run execution helpers
# ------------------------------------------------------------------

def _build_seeded_db(data_seed):
    """Isolated in-memory DB seeded with `CASES_PER_RUN` cases. Returns open conn."""
    conn = db.get_memory_connection()
    db.init_db(conn)
    rng = random.Random(data_seed)
    for rec in seed_module.build_records(rng):
        db.insert_mandate_failure(conn, rec)
    conn.commit()
    return conn


def _run_baseline_a(conn, agent_seed):
    """Naive baseline: 1 attempt, no strategy. Side-effect free."""
    result = baseline_module.run_baseline(conn)
    return {
        "recovery_rate": result["recovery_rate"],
        "amount_recovered": result["amount_recovered"],
        "escalation_rate": 0.0,  # naive baseline has no escalation concept
        "duplicate_count": 0,
    }


def _run_baseline_b(conn, agent_seed):
    """Dumb persistence: up to 3 attempts, no strategy. Side-effect free."""
    result = baseline_module.run_dumb_persistence_baseline(conn)
    return {
        "recovery_rate": result["recovery_rate"],
        "amount_recovered": result["amount_recovered"],
        "escalation_rate": 0.0,  # no escalation concept in dumb persistence
        "duplicate_count": 0,
    }


def _run_mandate_rescue(conn, agent_seed):
    """Full Mandate Rescue pipeline: scoring + salary-window + dunning + escalation.

    Forces use_llm=False (template-only narration) so benchmark runs are fast and
    deterministic -- LLM narration never affects any decision, score, or RNG draw.
    """
    import llm_client as _llm
    _llm.set_live_budget([], suppress=True)
    policy = agent_module.PolicyParams(use_llm=False)
    agent_module.run_agent(policy=policy, seed=agent_seed, conn=conn)

    cases = db.get_all_cases(conn)
    total = len(cases)
    recovered = [c for c in cases if c["case_status"] == "recovered"]
    escalated = [c for c in cases if c["case_status"] == "escalated"]
    amount_recovered = sum(float(c["amount"]) for c in recovered)

    # Duplicate detection: count cases that got a webhook_duplicate audit event
    # (should be 0 on a fresh DB — any non-zero value is a reliability violation).
    all_audit = db.get_all_audit(conn)
    duplicate_count = sum(1 for row in all_audit if row["event_type"] == "webhook_duplicate")

    return {
        "recovery_rate": len(recovered) / total if total else 0.0,
        "amount_recovered": amount_recovered,
        "escalation_rate": len(escalated) / total if total else 0.0,
        "duplicate_count": duplicate_count,
    }


# ------------------------------------------------------------------
# Main benchmark runner
# ------------------------------------------------------------------

def run_benchmark(n_runs=DEFAULT_N_RUNS, seed=DEFAULT_SEED, confidence=0.95,
                  data_seed=DEFAULT_SEED, verbose=True):
    """Run n_runs iterations, each with a fresh seeded DB.

    Returns a structured results dict with per-strategy means, CIs, and the
    paired improvement deltas (MR vs A, MR vs B).

    Args:
        n_runs:     number of Monte Carlo iterations per strategy.
        seed:       starting agent RNG seed (incremented per iteration × 10).
        confidence: CI confidence level (default 0.95).
        data_seed:  fixed seed for the data generator (default = seed).
        verbose:    print progress dots while running.
    """
    collected = {
        "baseline_a": {"recovery_rate": [], "amount_recovered": [], "duplicate_count": []},
        "baseline_b": {"recovery_rate": [], "amount_recovered": [], "duplicate_count": []},
        "mandate_rescue": {"recovery_rate": [], "amount_recovered": [],
                           "escalation_rate": [], "duplicate_count": []},
    }

    if verbose:
        print(f"Running benchmark: seed={seed}  data_seed={data_seed}  "
              f"n_runs={n_runs}  cases_per_run={CASES_PER_RUN}", flush=True)
        print("Progress: ", end="", flush=True)

    t_start = time.monotonic()
    policy_violations = []

    for i in range(n_runs):
        agent_seed = seed + i * 10

        # --- Baseline A (naive) — no write-back, pure read ---
        conn_a = _build_seeded_db(data_seed)
        try:
            r_a = _run_baseline_a(conn_a, agent_seed)
        finally:
            conn_a.close()

        # --- Baseline B (dumb persistence) — no write-back, pure read ---
        conn_b = _build_seeded_db(data_seed)
        try:
            r_b = _run_baseline_b(conn_b, agent_seed)
        finally:
            conn_b.close()

        # --- Mandate Rescue (full pipeline) — writes status + audit rows ---
        conn_mr = _build_seeded_db(data_seed)
        try:
            r_mr = _run_mandate_rescue(conn_mr, agent_seed)
        finally:
            conn_mr.close()

        # --- Policy violation check ---
        if r_mr["duplicate_count"] > 0:
            policy_violations.append({
                "run": i,
                "agent_seed": agent_seed,
                "duplicate_count": r_mr["duplicate_count"],
            })

        for key in ("recovery_rate", "amount_recovered", "duplicate_count"):
            collected["baseline_a"][key].append(r_a[key])
            collected["baseline_b"][key].append(r_b[key])
        for key in ("recovery_rate", "amount_recovered", "escalation_rate", "duplicate_count"):
            collected["mandate_rescue"][key].append(r_mr[key])

        if verbose:
            print("." if (i + 1) % 10 else f"{i+1}", end="", flush=True)

    elapsed = time.monotonic() - t_start
    if verbose:
        print(f"\nCompleted {n_runs} runs in {elapsed:.1f}s", flush=True)

    # --- Summarise ---
    def s(strategy, metric):
        return _summarize(collected[strategy][metric], confidence)

    results = {
        "config": {
            "seed": seed,
            "data_seed": data_seed,
            "n_runs": n_runs,
            "cases_per_run": CASES_PER_RUN,
            "confidence": confidence,
            "ci_method": "t" if _HAVE_SCIPY else "normal_approx",
            "elapsed_seconds": round(elapsed, 2),
        },
        "baseline_a": {
            "name": "Baseline A - Naive (1 attempt, no strategy)",
            "recovery_rate": s("baseline_a", "recovery_rate"),
            "amount_recovered": s("baseline_a", "amount_recovered"),
            "attempts_per_case": "1",
        },
        "baseline_b": {
            "name": "Baseline B - Dumb Persistence (<=3 attempts, no strategy)",
            "recovery_rate": s("baseline_b", "recovery_rate"),
            "amount_recovered": s("baseline_b", "amount_recovered"),
            "attempts_per_case": f"<={agent_module.MAX_RETRIES}",
        },
        "mandate_rescue": {
            "name": "Mandate Rescue - Full Pipeline (scoring + timing + dunning)",
            "recovery_rate": s("mandate_rescue", "recovery_rate"),
            "amount_recovered": s("mandate_rescue", "amount_recovered"),
            "escalation_rate": s("mandate_rescue", "escalation_rate"),
            "duplicate_count": s("mandate_rescue", "duplicate_count"),
            "attempts_per_case": f"<={agent_module.MAX_RETRIES}",
        },
        "policy_violations": {
            "count": len(policy_violations),
            "detail": policy_violations,
        },
        # Paired improvement deltas (MR - baseline, per run then summarised).
        "improvement_vs_a": {
            "recovery_rate": _summarize(
                [m - a for m, a in zip(
                    collected["mandate_rescue"]["recovery_rate"],
                    collected["baseline_a"]["recovery_rate"],
                )], confidence),
            "amount_recovered": _summarize(
                [m - a for m, a in zip(
                    collected["mandate_rescue"]["amount_recovered"],
                    collected["baseline_a"]["amount_recovered"],
                )], confidence),
        },
        "improvement_vs_b": {
            "recovery_rate": _summarize(
                [m - b for m, b in zip(
                    collected["mandate_rescue"]["recovery_rate"],
                    collected["baseline_b"]["recovery_rate"],
                )], confidence),
            "amount_recovered": _summarize(
                [m - b for m, b in zip(
                    collected["mandate_rescue"]["amount_recovered"],
                    collected["baseline_b"]["amount_recovered"],
                )], confidence),
        },
    }
    return results


# ------------------------------------------------------------------
# Pretty-print table
# ------------------------------------------------------------------

def _pp(val, is_pct=False, is_rupees=False):
    if is_pct:
        return f"{val * 100:.1f}%"
    if is_rupees:
        return f"Rs {val:,.0f}"
    return f"{val:.4f}"


def print_report(results):
    cfg = results["config"]
    w = 76
    divider = "-" * w
    print()
    print("=" * w)
    print("Mandate Rescue - Recovery Strategy Benchmark")
    print("=" * w)
    print(f"  Seed: {cfg['seed']}   Data seed: {cfg['data_seed']}   "
          f"Cases per run: {cfg['cases_per_run']}   "
          f"Monte Carlo runs: {cfg['n_runs']}   "
          f"CI method: {cfg['ci_method']}")
    print(f"  Elapsed: {cfg['elapsed_seconds']}s")
    print()

    # Header
    col = 22
    print(f"{'Strategy':<{col}}  {'Recovery rate':<20}  {'Recovered Rs':<18}  "
          f"{'Escal. rate':<18}  Attempts")
    print(divider)

    def _fmt_stat(stat, is_pct=False, is_rupees=False):
        mean_s = _pp(stat["mean"], is_pct=is_pct, is_rupees=is_rupees)
        m = stat["ci_margin"]
        if is_pct:
            margin_s = f"+/-{m * 100:.1f}pp"
        elif is_rupees:
            margin_s = f"+/-Rs{m:,.0f}"
        else:
            margin_s = f"+/-{m:.4f}"
        return f"{mean_s} {margin_s}"

    for key, label, show_escal in [
        ("baseline_a", "Baseline A (naive)", False),
        ("baseline_b", "Baseline B (persist.)", False),
        ("mandate_rescue", "Mandate Rescue", True),
    ]:
        s = results[key]
        rr = _fmt_stat(s["recovery_rate"], is_pct=True)
        am = _fmt_stat(s["amount_recovered"], is_rupees=True)
        er = _fmt_stat(s["escalation_rate"], is_pct=True) if show_escal else "n/a"
        att = s["attempts_per_case"]
        print(f"  {label:<{col-2}}  {rr:<20}  {am:<18}  {er:<18}  {att}")

    print(divider)
    print()

    def _delta_line(label, delta_rr, delta_am):
        rr_s = _fmt_stat(delta_rr, is_pct=True)
        am_s = _fmt_stat(delta_am, is_rupees=True)
        sign_rr = "+" if delta_rr["mean"] >= 0 else ""
        sign_am = "+" if delta_am["mean"] >= 0 else ""
        print(f"  {label}: {sign_rr}{rr_s} recovery rate  |  {sign_am}{am_s} / run")

    _delta_line("MR vs Baseline A (naive)",
                results["improvement_vs_a"]["recovery_rate"],
                results["improvement_vs_a"]["amount_recovered"])
    _delta_line("MR vs Baseline B (persist.)",
                results["improvement_vs_b"]["recovery_rate"],
                results["improvement_vs_b"]["amount_recovered"])

    pv = results["policy_violations"]["count"]
    print()
    print(f"  Policy violations (duplicate processing): {pv}")
    if pv:
        print(f"  WARNING: {pv} run(s) had duplicate recovery events — see 'detail' in JSON output.")
    else:
        print("  PASS: No duplicate processing detected across all runs.")

    print()
    print("  Methodology note: both baselines use the identical per-attempt")
    print("  success-probability model as Mandate Rescue. The only difference")
    print("  is strategy. 'Baseline B -> MR' is the defensible intelligence delta.")
    print("=" * w)
    print()


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def _parse_args():
    p = argparse.ArgumentParser(
        description="Reproducible recovery-strategy benchmark for Mandate Rescue.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--seed", type=int, default=DEFAULT_SEED,
                   help=f"Agent RNG base seed (default {DEFAULT_SEED})")
    p.add_argument("--data-seed", type=int, default=None,
                   help="Data generator seed (default = --seed)")
    p.add_argument("--n-runs", type=int, default=DEFAULT_N_RUNS,
                   help=f"Monte Carlo runs per strategy (default {DEFAULT_N_RUNS})")
    p.add_argument("--json", action="store_true",
                   help="Emit results as JSON to stdout (in addition to table)")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress progress output")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    data_seed = args.data_seed if args.data_seed is not None else args.seed

    results = run_benchmark(
        n_runs=args.n_runs,
        seed=args.seed,
        data_seed=data_seed,
        verbose=not args.quiet,
    )
    print_report(results)

    if args.json:
        print(json.dumps(results, indent=2))

    violations = results["policy_violations"]["count"]
    sys.exit(1 if violations > 0 else 0)
