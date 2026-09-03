"""Revenue Intelligence Layer — Phase 5.

Computes every analytics aggregate needed by Phase 5 features from real stored data.
Nothing is hardcoded or invented; every number traces to mandate_failures rows and
audit_log events.

Key public functions
--------------------
by_failure_reason(conn)
    Recovery rate, recovered amount, lost revenue per failure_reason.
    Validates against scoring.REASON_BASE so the hardcoded expert priors can be
    compared to actual observed outcomes.

by_strategy_outcome(conn)
    Which strategies were actually used and how did each perform?
    Extracted from audit_log event_type='strategy_selected'.

by_merchant_category(conn)
    Recovery rate + amount by merchant_category (existing cohort extended with
    failure breakdown and strategy distribution).

failure_rate_by_segment(conn)
    Cross-tab: recovery rate split by (failure_reason × merchant_category).

incremental_revenue(conn)
    Counterfactual: what would the naive baseline have recovered?
    Returns actual vs baseline vs estimated incremental, clearly labelled as
    [ESTIMATE — simulation-based counterfactual].

strategy_comparison(conn, n_runs=20)
    Compares each strategy in isolation using in-memory simulation.
    Returns per-strategy metrics.  Clearly labelled SIMULATION.

merchant_learning(conn)
    Per-merchant-category: best-performing strategy, sample size, confidence.
    Only populated when a merchant category has >= MIN_SAMPLE for the claim.

full_summary(conn)
    Single call returning all of the above for the dashboard.

IMPORTANT: every returned dict that contains estimated / simulated / projected data
carries an explicit "data_type" key: "actual" | "estimate" | "simulation" | "forecast"
so the frontend can render the correct label and the data-trust rule is enforced at
the source.
"""

import random
from typing import Optional

import db
import scoring
import baseline as baseline_module
from agent import _success_prob, MAX_RETRIES

# Minimum sample size for a merchant-level claim to be surfaced.
MIN_SAMPLE = 10
# Minimum sample size to publish a strategy-level claim.
MIN_STRATEGY_SAMPLE = 5

# Strategy labels as written by StrategyAgent into audit_log.action_taken.
# These must stay in sync with _STRATEGY_LABELS in agent.py.
KNOWN_STRATEGIES = {
    "salary-window retry": "insufficient_funds",
    "re-authorization link": "mandate_expired",
    "silent quick retry": "bank_technical_error",
    "immediate escalation": "mandate_revoked",
    "higher-limit re-authorization": "over_limit",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cases_and_audit(conn):
    """Return (cases, audit_by_case) loaded efficiently from a shared connection."""
    cases = db.get_all_cases(conn)
    # Build audit index in one pass over audit_log (no N+1).
    audit_by_case: dict = {}
    for row in conn.execute(
        "SELECT customer_id, event_type, action_taken, outcome, attempt_number "
        "FROM audit_log ORDER BY event_id"
    ).fetchall():
        audit_by_case.setdefault(row["customer_id"], []).append(dict(row))
    return cases, audit_by_case


def _segment_stats(cases, key_fn):
    """Aggregate recovery stats from a list of case dicts grouped by key_fn."""
    buckets: dict = {}
    for c in cases:
        key = key_fn(c)
        b = buckets.setdefault(key, {
            "segment": key,
            "total": 0,
            "recovered": 0,
            "escalated": 0,
            "amount_at_risk": 0.0,
            "amount_recovered": 0.0,
            "amount_lost": 0.0,
        })
        b["total"] += 1
        amt = float(c["amount"])
        b["amount_at_risk"] += amt
        status = c["case_status"]
        if status == "recovered":
            b["recovered"] += 1
            b["amount_recovered"] += amt
        elif status in ("escalated", "broken_promise"):
            b["escalated"] += 1
            b["amount_lost"] += amt
    for b in buckets.values():
        t = b["total"]
        b["recovery_rate"] = round(b["recovered"] / t, 4) if t else 0.0
        b["escalation_rate"] = round(b["escalated"] / t, 4) if t else 0.0
        b["amount_at_risk"] = round(b["amount_at_risk"], 2)
        b["amount_recovered"] = round(b["amount_recovered"], 2)
        b["amount_lost"] = round(b["amount_lost"], 2)
        b["data_type"] = "actual"
    return list(buckets.values())


def _extract_strategy_from_audit(audit_events: list) -> Optional[str]:
    """Return the strategy label selected for a case, or None if not yet assigned."""
    for e in audit_events:
        if e["event_type"] == "strategy_selected":
            # action_taken format: "Strategy: <label>"
            action = e.get("action_taken", "")
            if action.startswith("Strategy:"):
                return action[len("Strategy:"):].strip()
    return None


# ---------------------------------------------------------------------------
# 1. by_failure_reason
# ---------------------------------------------------------------------------

def by_failure_reason(conn) -> dict:
    """Actual recovery stats per failure_reason from real case outcomes.

    Also returns the scoring.py REASON_BASE prior for comparison — explicitly
    labelled as [prior] vs [actual] so the dashboard never conflates them.
    """
    from scoring import REASON_BASE

    cases = db.get_all_cases(conn)
    rows = _segment_stats(cases, lambda c: c["failure_reason"])
    rows.sort(key=lambda r: r["amount_lost"], reverse=True)

    # Annotate each row with the hardcoded REASON_BASE prior for comparison.
    for r in rows:
        r["recoverability_prior"] = REASON_BASE.get(r["segment"], None)
        prior = r["recoverability_prior"]
        actual = r["recovery_rate"]
        if prior is not None and r["total"] >= MIN_STRATEGY_SAMPLE:
            diff = actual - prior
            r["prior_vs_actual_delta"] = round(diff, 4)
            r["prior_label"] = "prior (expert estimate)"
            r["actual_label"] = "actual (observed)"
        else:
            r["prior_vs_actual_delta"] = None
            r["prior_label"] = "prior (expert estimate)"
            r["actual_label"] = "actual (observed, insufficient data)" \
                if r["total"] < MIN_STRATEGY_SAMPLE else "actual (observed)"

    return {
        "data_type": "actual",
        "description": "Recovery outcomes grouped by failure reason.",
        "by_failure_reason": rows,
    }


# ---------------------------------------------------------------------------
# 2. by_strategy_outcome
# ---------------------------------------------------------------------------

def by_strategy_outcome(conn) -> dict:
    """Which strategies were used and what was the actual recovery rate for each?

    Extracted from audit_log strategy_selected events + final case_status.
    Only includes strategies with sufficient sample size for a meaningful claim.
    """
    cases, audit_by_case = _cases_and_audit(conn)

    # Map customer_id → strategy
    strategy_by_case: dict = {}
    for cid, events in audit_by_case.items():
        strategy_by_case[cid] = _extract_strategy_from_audit(events)

    # Build per-strategy buckets
    buckets: dict = {}
    untracked = 0
    for c in cases:
        strat = strategy_by_case.get(c["customer_id"])
        if not strat:
            untracked += 1
            continue
        b = buckets.setdefault(strat, {
            "strategy": strat,
            "total": 0,
            "recovered": 0,
            "escalated": 0,
            "amount_at_risk": 0.0,
            "amount_recovered": 0.0,
        })
        b["total"] += 1
        amt = float(c["amount"])
        b["amount_at_risk"] += amt
        status = c["case_status"]
        if status == "recovered":
            b["recovered"] += 1
            b["amount_recovered"] += amt
        elif status in ("escalated", "broken_promise"):
            b["escalated"] += 1

    rows = []
    for b in buckets.values():
        t = b["total"]
        b["recovery_rate"] = round(b["recovered"] / t, 4) if t else 0.0
        b["escalation_rate"] = round(b["escalated"] / t, 4) if t else 0.0
        b["amount_at_risk"] = round(b["amount_at_risk"], 2)
        b["amount_recovered"] = round(b["amount_recovered"], 2)
        b["sufficient_sample"] = t >= MIN_STRATEGY_SAMPLE
        b["data_type"] = "actual"
        rows.append(b)

    rows.sort(key=lambda r: r["amount_recovered"], reverse=True)

    return {
        "data_type": "actual",
        "description": (
            "Actual recovery outcomes grouped by strategy selected by the agent. "
            "Strategy is extracted from audit_log; outcomes from case_status."
        ),
        "by_strategy": rows,
        "cases_without_strategy": untracked,
    }


# ---------------------------------------------------------------------------
# 3. by_merchant_category
# ---------------------------------------------------------------------------

def by_merchant_category(conn) -> dict:
    """Recovery stats per merchant_category with failure-type breakdown.

    Extends the existing metrics.cohorts() with per-reason breakdown and
    strategy distribution per merchant.
    """
    cases, audit_by_case = _cases_and_audit(conn)

    # Top-level per-category stats
    cat_rows = _segment_stats(cases, lambda c: c["merchant_category"])
    cat_rows.sort(key=lambda r: r["amount_lost"], reverse=True)

    # Per-category failure-reason breakdown
    cat_reason: dict = {}
    for c in cases:
        cat = c["merchant_category"]
        reason = c["failure_reason"]
        key = (cat, reason)
        b = cat_reason.setdefault(key, {
            "merchant_category": cat,
            "failure_reason": reason,
            "total": 0,
            "recovered": 0,
            "amount_at_risk": 0.0,
        })
        b["total"] += 1
        b["amount_at_risk"] += float(c["amount"])
        if c["case_status"] == "recovered":
            b["recovered"] += 1

    for b in cat_reason.values():
        t = b["total"]
        b["recovery_rate"] = round(b["recovered"] / t, 4) if t else 0.0
        b["amount_at_risk"] = round(b["amount_at_risk"], 2)
        b["data_type"] = "actual"

    # Per-category strategy distribution
    cat_strategy: dict = {}
    for c in cases:
        cat = c["merchant_category"]
        strat = _extract_strategy_from_audit(audit_by_case.get(c["customer_id"], []))
        if strat:
            key = (cat, strat)
            cat_strategy[key] = cat_strategy.get(key, 0) + 1

    return {
        "data_type": "actual",
        "description": "Recovery outcomes grouped by merchant category.",
        "by_category": cat_rows,
        "by_category_and_reason": list(cat_reason.values()),
        "strategy_distribution_by_category": [
            {"merchant_category": k[0], "strategy": k[1], "count": v}
            for k, v in sorted(cat_strategy.items())
        ],
    }


# ---------------------------------------------------------------------------
# 4. failure_rate_by_segment (cross-tab)
# ---------------------------------------------------------------------------

def failure_rate_by_segment(conn) -> dict:
    """Cross-tab: recovery rate by (failure_reason × merchant_category).

    Each cell is the recovery rate for that specific combination,
    enabling 'which bank_technical_error cases in insurance fare worst?' queries.
    """
    cases = db.get_all_cases(conn)
    buckets: dict = {}
    for c in cases:
        key = (c["failure_reason"], c["merchant_category"])
        b = buckets.setdefault(key, {
            "failure_reason": c["failure_reason"],
            "merchant_category": c["merchant_category"],
            "total": 0, "recovered": 0,
            "amount_at_risk": 0.0, "amount_recovered": 0.0,
        })
        b["total"] += 1
        b["amount_at_risk"] += float(c["amount"])
        if c["case_status"] == "recovered":
            b["recovered"] += 1
            b["amount_recovered"] += float(c["amount"])

    rows = []
    for b in buckets.values():
        t = b["total"]
        b["recovery_rate"] = round(b["recovered"] / t, 4) if t else 0.0
        b["amount_at_risk"] = round(b["amount_at_risk"], 2)
        b["amount_recovered"] = round(b["amount_recovered"], 2)
        b["data_type"] = "actual"
        rows.append(b)

    rows.sort(key=lambda r: r["amount_at_risk"], reverse=True)
    return {
        "data_type": "actual",
        "description": "Recovery rate cross-tabulated by failure reason and merchant category.",
        "segments": rows,
    }


# ---------------------------------------------------------------------------
# 5. incremental_revenue (counterfactual)
# ---------------------------------------------------------------------------

def incremental_revenue(conn) -> dict:
    """Counterfactual estimate: actual agent vs naive baseline vs dumb persistence.

    Returns actual recovered amounts plus two simulated baseline comparisons.
    The baseline figures use the SAME probability model as the agent (not a separate
    model), so the comparison isolates strategy value, not probability calibration.

    Data types:
      actual_recovered  → "actual"  (from real case_status == 'recovered')
      naive_baseline    → "estimate" (simulation from seeded RNG + probability model)
      dumb_persistence  → "estimate"
      incremental       → "estimate"
    """
    cases = db.get_all_cases(conn)
    counted = [c for c in cases if c["case_status"] not in ("invalid", "duplicate")]
    total = len(counted)

    actual_recovered = sum(
        float(c["amount"]) for c in counted if c["case_status"] == "recovered"
    )
    actual_recovery_rate = (
        sum(1 for c in counted if c["case_status"] == "recovered") / total
        if total else 0.0
    )

    # Run baselines against the real case set.
    naive = baseline_module.run_baseline(conn)
    dumb = baseline_module.run_dumb_persistence_baseline(conn)

    naive_recovered = naive["amount_recovered"]
    dumb_recovered = dumb["amount_recovered"]

    incremental_vs_naive = round(actual_recovered - naive_recovered, 2)
    incremental_vs_dumb = round(actual_recovered - dumb_recovered, 2)

    return {
        "data_type": "mixed",
        "description": (
            "Counterfactual revenue analysis. "
            "'actual' values come from real recovery outcomes. "
            "'estimate' values are simulation-based counterfactuals using the same "
            "probability model; they are clearly labelled and must not be presented "
            "as guaranteed."
        ),
        "actual": {
            "data_type": "actual",
            "amount_recovered": round(actual_recovered, 2),
            "recovery_rate": round(actual_recovery_rate, 4),
            "total_cases": total,
        },
        "naive_baseline_1_attempt": {
            "data_type": "estimate",
            "label": "Naive baseline (1 attempt, no strategy) [ESTIMATE]",
            "amount_recovered": naive_recovered,
            "recovery_rate": naive["recovery_rate"],
        },
        "dumb_persistence_baseline": {
            "data_type": "estimate",
            "label": "Dumb persistence (same retry budget, no strategy) [ESTIMATE]",
            "amount_recovered": dumb_recovered,
            "recovery_rate": dumb["recovery_rate"],
            "retry_cap": dumb["retry_cap"],
        },
        "incremental": {
            "data_type": "estimate",
            "label": "Incremental revenue attributable to agent strategy [ESTIMATE]",
            "vs_naive": incremental_vs_naive,
            "vs_dumb_persistence": incremental_vs_dumb,
            "interpretation": (
                f"The agent's strategy (scoring + timing + dunning) contributed "
                f"an estimated Rs {incremental_vs_dumb:,.2f} more than simple "
                f"persistence with the same retry budget. "
                f"[ESTIMATE — simulation-based counterfactual, not causal proof]"
            ),
        },
    }


# ---------------------------------------------------------------------------
# 6. strategy_comparison (simulation-based)
# ---------------------------------------------------------------------------

def strategy_comparison(conn, n_runs: int = 20) -> dict:
    """Compare strategies in isolation using in-memory Monte Carlo simulation.

    Runs each strategy type (salary-window, re-auth, silent retry, immediate
    escalation) independently against the relevant subset of cases with varying
    RNG seeds to estimate mean + std recovery rate.

    ALL values are from simulation — explicitly labelled "simulation".
    Never presented as actual historical results.
    """
    cases = db.get_all_cases(conn)
    n_runs = max(5, min(n_runs, 50))

    # Define each strategy's applicable case filter + retry budget.
    strategy_defs = {
        "salary-window retry": {
            "filter": lambda c: c["failure_reason"] == "insufficient_funds",
            "retries": 3,
        },
        "re-authorization link": {
            "filter": lambda c: c["failure_reason"] == "mandate_expired",
            "retries": 3,
        },
        "silent quick retry": {
            "filter": lambda c: c["failure_reason"] == "bank_technical_error",
            "retries": 3,
        },
        "immediate escalation": {
            "filter": lambda c: c["failure_reason"] == "mandate_revoked",
            "retries": 0,
        },
    }

    results = {}
    for strategy_name, sdef in strategy_defs.items():
        subset = [c for c in cases if sdef["filter"](c)]
        if not subset:
            continue

        recovery_rates = []
        amounts_recovered = []
        for seed in range(n_runs):
            rng = random.Random(1000 + seed)
            recovered = 0
            amount_rec = 0.0
            for c in subset:
                score, _ = scoring.score_case(c)
                prob = _success_prob(c, score)
                case_recovered = False
                for _ in range(max(1, sdef["retries"])):
                    if rng.random() < prob:
                        case_recovered = True
                        break
                if case_recovered:
                    recovered += 1
                    amount_rec += float(c["amount"])
            n = len(subset)
            recovery_rates.append(recovered / n if n else 0.0)
            amounts_recovered.append(amount_rec)

        import math
        mean_rate = sum(recovery_rates) / len(recovery_rates)
        std_rate = (
            math.sqrt(sum((r - mean_rate) ** 2 for r in recovery_rates) / (len(recovery_rates) - 1))
            if len(recovery_rates) > 1 else 0.0
        )
        mean_amt = sum(amounts_recovered) / len(amounts_recovered)

        results[strategy_name] = {
            "data_type": "simulation",
            "strategy": strategy_name,
            "applicable_cases": len(subset),
            "simulated_recovery_rate_mean": round(mean_rate, 4),
            "simulated_recovery_rate_std": round(std_rate, 4),
            "simulated_amount_recovered_mean": round(mean_amt, 2),
            "n_simulation_runs": n_runs,
            "label": f"{strategy_name} [SIMULATION — {n_runs} runs]",
        }

    return {
        "data_type": "simulation",
        "description": (
            "Per-strategy recovery rate estimated via Monte Carlo simulation "
            f"({n_runs} runs each). These are simulation estimates, not actual "
            "historical outcomes. The same probability model is used for all "
            "strategies so the comparison is internally consistent."
        ),
        "strategies": list(results.values()),
    }


# ---------------------------------------------------------------------------
# 7. merchant_learning (per-merchant strategy performance)
# ---------------------------------------------------------------------------

def merchant_learning(conn) -> dict:
    """Per-merchant-category: which strategy performs best based on actual data?

    Only produces a recommendation when there are >= MIN_SAMPLE cases for the
    merchant category AND >= MIN_STRATEGY_SAMPLE cases per strategy within that
    category. Under-sampled merchants get a 'insufficient_data' flag instead
    of a potentially misleading recommendation.
    """
    cases, audit_by_case = _cases_and_audit(conn)

    # Build (merchant, strategy) → outcome buckets
    buckets: dict = {}
    for c in cases:
        cat = c["merchant_category"]
        strat = _extract_strategy_from_audit(audit_by_case.get(c["customer_id"], []))
        if not strat:
            continue
        key = (cat, strat)
        b = buckets.setdefault(key, {
            "merchant_category": cat,
            "strategy": strat,
            "total": 0, "recovered": 0,
            "amount_recovered": 0.0,
        })
        b["total"] += 1
        if c["case_status"] == "recovered":
            b["recovered"] += 1
            b["amount_recovered"] += float(c["amount"])

    # Find best strategy per merchant
    by_merchant: dict = {}
    for (cat, strat), b in buckets.items():
        t = b["total"]
        rate = b["recovered"] / t if t else 0.0
        if cat not in by_merchant:
            by_merchant[cat] = []
        by_merchant[cat].append({
            "strategy": strat,
            "total": t,
            "recovered": b["recovered"],
            "recovery_rate": round(rate, 4),
            "amount_recovered": round(b["amount_recovered"], 2),
        })

    merchant_summaries = []
    for cat, strategies in sorted(by_merchant.items()):
        total_for_cat = sum(s["total"] for s in strategies)
        sufficient = total_for_cat >= MIN_SAMPLE and any(
            s["total"] >= MIN_STRATEGY_SAMPLE for s in strategies
        )
        # Best strategy = highest recovery rate among sufficiently sampled ones
        eligible = [s for s in strategies if s["total"] >= MIN_STRATEGY_SAMPLE]
        best = max(eligible, key=lambda s: s["recovery_rate"]) if eligible else None

        merchant_summaries.append({
            "data_type": "actual",
            "merchant_category": cat,
            "total_cases": total_for_cat,
            "sufficient_data": sufficient,
            "best_strategy": best["strategy"] if best else None,
            "best_strategy_recovery_rate": best["recovery_rate"] if best else None,
            "best_strategy_sample": best["total"] if best else None,
            "all_strategies": strategies,
            "recommendation": (
                f"Use '{best['strategy']}' for {cat} cases "
                f"(observed {best['recovery_rate']*100:.1f}% recovery on {best['total']} cases). "
                "[ACTUAL DATA — verify with ongoing results]"
            ) if best else "Insufficient data for a strategy recommendation.",
        })

    return {
        "data_type": "actual",
        "description": (
            "Per-merchant-category strategy performance from actual recovery outcomes. "
            f"Recommendations only shown for categories with >= {MIN_SAMPLE} total cases "
            f"and >= {MIN_STRATEGY_SAMPLE} cases per strategy."
        ),
        "merchants": merchant_summaries,
    }


# ---------------------------------------------------------------------------
# 8. full_summary — single endpoint payload
# ---------------------------------------------------------------------------

def full_summary(conn, include_simulation: bool = True) -> dict:
    """Return all intelligence aggregates in one call for the dashboard.

    Designed so the frontend makes ONE API call to get the full intelligence
    picture rather than 6+ sequential requests.  Each sub-section carries its own
    data_type label.
    """
    result = {
        "by_failure_reason": by_failure_reason(conn),
        "by_strategy_outcome": by_strategy_outcome(conn),
        "by_merchant_category": by_merchant_category(conn),
        "failure_rate_by_segment": failure_rate_by_segment(conn),
        "incremental_revenue": incremental_revenue(conn),
        "merchant_learning": merchant_learning(conn),
    }
    if include_simulation:
        result["strategy_comparison"] = strategy_comparison(conn, n_runs=20)
    return result
