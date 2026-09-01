"""Monte Carlo policy simulation runner for the Policy Experimentation Sandbox.

WHAT THIS IS
------------
An *analysis tool*. It runs the full recovery pipeline many times under a given
PolicyParams to measure how sensitive the recovery/escalation/amount outcomes are to
policy choices (retry cap, score weights, salary-window mode). It reports each metric
as a mean with a 95% confidence interval so a claimed improvement can be stated
honestly as "X% +/- Y%" rather than a single lucky number.

It does NOT change the live agent's configuration and never writes to the real
`mandate_rescue.db`. Each run happens in its own isolated in-memory SQLite database
seeded with the same 180 synthetic cases (via seed.build_records) but driven by a
different agent RNG seed, so the runs are independent samples of the same policy.

PERFORMANCE TRADEOFF (documented as required)
---------------------------------------------
A real remote LLM call per case would make 30 full runs far too slow. The LLM is
narration-only (it never changes a decision, score, or RNG draw), so Monte Carlo runs
set `use_llm=False` and use the deterministic templates instead. This was verified: a
default-policy run is byte-for-byte identical in recovered/escalated counts whether the
LLM is on or off. Only the single "live demo" run in the dashboard uses the real LLM;
these repeated simulation runs never do. This is surfaced in the UI too.
"""

import math
import random

import db
import seed as seed_module
import agent as agent_module

# scipy gives an exact t/normal critical value; fall back to the standard normal
# approximation (z = 1.96 for 95%) if scipy is not installed. Both are documented as
# acceptable by the spec.
try:
    from scipy import stats as _scipy_stats
    _HAVE_SCIPY = True
except Exception:  # pragma: no cover - scipy optional
    _scipy_stats = None
    _HAVE_SCIPY = False

# Base seed for the sequence of per-run agent seeds. Using base + i keeps the whole
# Monte Carlo experiment reproducible while giving each run a distinct RNG stream.
MONTE_CARLO_BASE_SEED = 1000

# The metrics we collect from each run.
METRIC_KEYS = ("recovery_rate", "escalation_rate", "amount_recovered")


def _build_memory_db():
    """Create an isolated in-memory SQLite DB seeded with the standard 180 cases.

    Returns an open sqlite3 connection the caller must close. The seed RNG is fixed
    (seed_module.SEED) so every Monte Carlo run starts from the identical case set;
    only the *agent's* RNG seed varies between runs.
    """
    conn = db.get_memory_connection()
    db.init_db(conn)
    rng = random.Random(seed_module.SEED)
    records = seed_module.build_records(rng)
    for record in records:
        db.insert_mandate_failure(conn, record)
    conn.commit()
    return conn


def _run_once(policy, seed):
    """Run the full pipeline once in an isolated DB; return one metrics sample dict."""
    conn = _build_memory_db()
    try:
        agent_module.run_agent(policy=policy, seed=seed, conn=conn)
        cases = db.get_all_cases(conn)
        total = len(cases)
        recovered = [c for c in cases if c["case_status"] == "recovered"]
        escalated = [c for c in cases if c["case_status"] == "escalated"]
        amount_recovered = sum(float(c["amount"]) for c in recovered)
        return {
            "recovery_rate": (len(recovered) / total) if total else 0.0,
            "escalation_rate": (len(escalated) / total) if total else 0.0,
            "amount_recovered": amount_recovered,
        }
    finally:
        conn.close()


def _critical_value(n, confidence=0.95):
    """Two-sided critical multiplier for the CI.

    Uses Student's t (n-1 df) via scipy when available (more honest for small n like
    30); otherwise the standard normal approximation z=1.96 for 95%.
    """
    if n <= 1:
        return 0.0
    alpha = 1.0 - confidence
    if _HAVE_SCIPY:
        return float(_scipy_stats.t.ppf(1.0 - alpha / 2.0, df=n - 1))
    # Standard normal approximation for the common 95% case.
    return 1.959963984540054 if abs(confidence - 0.95) < 1e-9 else 1.959963984540054


def _summarize(samples, confidence=0.95):
    """Return {mean, std, ci_low, ci_high, ci_margin, n} for one metric's samples.

    std is the sample standard deviation (ddof=1). The confidence interval is the
    standard mean +/- crit * (std / sqrt(n)) interval.
    """
    n = len(samples)
    if n == 0:
        return {"mean": 0.0, "std": 0.0, "ci_low": 0.0, "ci_high": 0.0,
                "ci_margin": 0.0, "n": 0}
    mean = sum(samples) / n
    if n == 1:
        return {"mean": mean, "std": 0.0, "ci_low": mean, "ci_high": mean,
                "ci_margin": 0.0, "n": 1}
    variance = sum((x - mean) ** 2 for x in samples) / (n - 1)
    std = math.sqrt(variance)
    crit = _critical_value(n, confidence)
    margin = crit * (std / math.sqrt(n))
    return {
        "mean": mean,
        "std": std,
        "ci_low": mean - margin,
        "ci_high": mean + margin,
        "ci_margin": margin,
        "n": n,
    }


def run_monte_carlo(n_runs=30, policy_params=None, confidence=0.95, base_seed=None):
    """Run the pipeline `n_runs` times under `policy_params`; return summary stats.

    Args:
        n_runs: number of independent simulation runs (default 30). Each uses a
            distinct agent RNG seed (base_seed + i) so the runs are independent
            samples of the same policy.
        policy_params: an agent.PolicyParams. Defaults to the live default policy.
            use_llm is FORCED to False here regardless of the passed value, because
            these are repeated internal simulations (see the module docstring's
            performance-tradeoff note); the LLM is narration-only and never affects
            outcomes.
        confidence: CI confidence level (default 0.95 -> 95% interval).
        base_seed: starting seed for the per-run RNG sequence (default
            MONTE_CARLO_BASE_SEED), exposed so the default and modified policies in a
            comparison can be run over the SAME seed set for a fair paired comparison.

    Returns a dict:
        {
          "n_runs", "confidence",
          "policy": {retry_cap, score_weights, salary_window_mode, is_default},
          "metrics": { <metric>: {mean, std, ci_low, ci_high, ci_margin, n} ... },
          "used_llm": False, "critical_method": "t"|"normal",
          "raw": { <metric>: [per-run values] }
        }
    """
    policy = policy_params or agent_module.DEFAULT_POLICY
    # Force template-only for repeated runs (performance; LLM is narration-only).
    policy = agent_module.replace(policy, use_llm=False)
    if base_seed is None:
        base_seed = MONTE_CARLO_BASE_SEED

    n_runs = max(1, int(n_runs))
    collected = {k: [] for k in METRIC_KEYS}
    for i in range(n_runs):
        sample = _run_once(policy, seed=base_seed + i)
        for k in METRIC_KEYS:
            collected[k].append(sample[k])

    metrics = {k: _summarize(collected[k], confidence) for k in METRIC_KEYS}
    return {
        "n_runs": n_runs,
        "confidence": confidence,
        "policy": {
            "retry_cap": policy.retry_cap,
            "score_weights": policy.normalized_weights(),
            "salary_window_mode": policy.salary_window_mode,
            "is_default": policy.is_default(),
        },
        "metrics": metrics,
        "used_llm": False,
        "critical_method": "t" if _HAVE_SCIPY else "normal",
        "raw": collected,
    }


def _diff_summary(mod_samples, base_samples, confidence=0.95):
    """Summarize the paired per-run difference (modified - default) for one metric.

    Because the modified and default policies are run over the SAME per-run seeds, the
    differences are paired, so we take the CI of the per-run deltas directly. This
    yields a tighter, more honest "improvement of X +/- Y" statement than comparing two
    independent intervals.
    """
    n = min(len(mod_samples), len(base_samples))
    deltas = [mod_samples[i] - base_samples[i] for i in range(n)]
    return _summarize(deltas, confidence)


def compare_policies(modified_policy, n_runs=30, confidence=0.95, base_seed=None):
    """Run the default policy and a modified policy over the SAME seeds and diff them.

    Returns:
        {
          "n_runs", "confidence",
          "default": <run_monte_carlo summary for the default policy>,
          "modified": <run_monte_carlo summary for the modified policy>,
          "delta": { <metric>: {mean, std, ci_low, ci_high, ci_margin, n} }  # paired
        }

    The paired delta lets the UI say "this change improves recovery by X% +/- Y%"
    honestly, with its own confidence interval.
    """
    if base_seed is None:
        base_seed = MONTE_CARLO_BASE_SEED
    default_summary = run_monte_carlo(n_runs, agent_module.DEFAULT_POLICY,
                                      confidence=confidence, base_seed=base_seed)
    modified_summary = run_monte_carlo(n_runs, modified_policy,
                                       confidence=confidence, base_seed=base_seed)
    delta = {}
    for k in METRIC_KEYS:
        delta[k] = _diff_summary(modified_summary["raw"][k],
                                 default_summary["raw"][k], confidence)
    # Drop the bulky raw arrays from the nested summaries before returning to the API.
    default_out = {kk: vv for kk, vv in default_summary.items() if kk != "raw"}
    modified_out = {kk: vv for kk, vv in modified_summary.items() if kk != "raw"}
    return {
        "n_runs": n_runs,
        "confidence": confidence,
        "default": default_out,
        "modified": modified_out,
        "delta": delta,
    }


if __name__ == "__main__":
    import json
    summary = run_monte_carlo(30)
    rr = summary["metrics"]["recovery_rate"]
    print(json.dumps({"recovery_rate": rr, "used_llm": summary["used_llm"],
                      "method": summary["critical_method"]}, indent=2))
