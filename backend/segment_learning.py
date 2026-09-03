"""Segment Learning — Phase 6.

Answers: "What strategy works best for a given case, given all available evidence?"

Implements a fallback hierarchy:
  1. merchant-specific      (strategy × merchant_category)
  2. failure-reason-specific (strategy × failure_reason)
  3. global                  (strategy × 'all')
  4. rule-based default      (no observed data at all)

Each level is only used if it has sufficient sample size (MIN_SAMPLE).
When the best level is used, the output carries the level name and sample size
so the caller can display "based on 47 merchant-specific observations" vs
"based on 12 global observations (insufficient merchant data)".

Data provenance:
  All lookups draw from strategy_performance, which carries provenance tags.
  The caller can request a specific provenance or accept the union.
  REAL_TEST and SIMULATION are kept separate when the caller asks for it.

Public API
----------
best_strategy_for_case(conn, case, require_provenance=None) → dict
strategy_ranking(conn, dimension_key, dimension_value) → list[dict]
full_learning_summary(conn) → dict
"""

from __future__ import annotations

import json
from typing import Optional

import db
from outcome_attribution import PROV_REAL_TEST, PROV_SIMULATION, PROV_HISTORICAL
from adaptive_policy import _rule_based_strategy

# Minimum attempts in a bucket to trust its recovery rate.
MIN_SAMPLE = 10

# Minimum improvement (percentage points) over the default strategy to recommend
# a different one. Guards against noise-driven switches.
MIN_IMPROVEMENT_PP = 3.0   # 3 percentage points


def _recovery_rate(row: dict) -> float:
    attempts = row.get("attempts", 0)
    if attempts == 0:
        return 0.0
    return row.get("recoveries", 0) / attempts


def _get_rates_for_dimension(conn, dimension_key: str, dimension_value: str,
                               provenance: Optional[str] = None) -> list[dict]:
    """Return strategy_performance rows for a specific dimension, optionally
    filtered by provenance. Enriches with computed recovery_rate."""
    rows = db.get_strategy_performance(
        conn,
        dimension_key=dimension_key,
        dimension_value=dimension_value,
        provenance=provenance,
    )
    for r in rows:
        r["recovery_rate"] = round(_recovery_rate(r), 4)
        r["sufficient"] = r["attempts"] >= MIN_SAMPLE
    return rows


def _best_from_rows(rows: list[dict]) -> Optional[dict]:
    """Return the row with the highest recovery_rate among those with sufficient data."""
    eligible = [r for r in rows if r.get("sufficient")]
    if not eligible:
        return None
    return max(eligible, key=lambda r: r["recovery_rate"])


def best_strategy_for_case(
    conn,
    case: dict,
    require_provenance: Optional[str] = None,
) -> dict:
    """Find the best strategy for a case using the fallback hierarchy.

    Args:
        case: mandate_failures row dict
        require_provenance: if set (e.g. PROV_REAL_TEST), only rows with that
            provenance are considered. Useful to ask "what do REAL observations say?"

    Returns a dict with:
        recommended_strategy  str
        confidence_level      str  (merchant_specific | failure_reason | global | rule_based)
        sample_size           int
        recovery_rate         float (0–1)
        fallback_used         bool
        explain               list of {level, strategy, rate, n, used}
        data_type             str
        provenance            str | None (filter that was applied)
    """
    merchant_cat = case.get("merchant_category", "unknown")
    fail_reason  = case.get("failure_reason", "unknown")
    rule_default = _rule_based_strategy(case)

    # Governance safety: mandate_revoked must ALWAYS use immediate escalation.
    # This rule cannot be overridden by observed data, no matter how good the
    # alternative strategy looks in the DB.
    if fail_reason == "mandate_revoked":
        return _result(
            strategy="immediate escalation",
            level="rule_based",
            sample_size=0,
            recovery_rate=None,
            fallback_used=False,
            explain=[{
                "level": "rule_based",
                "used": True,
                "strategy": "immediate escalation",
                "reason": "mandate_revoked: retry is blocked by policy regardless of observed data.",
            }],
            provenance=require_provenance,
            data_type="rule_based",
        )

    explain = []

    # --- Level 1: merchant-specific ---
    merchant_rows = _get_rates_for_dimension(
        conn, "merchant_category", merchant_cat, provenance=require_provenance
    )
    merchant_best = _best_from_rows(merchant_rows)
    explain.append({
        "level": "merchant_specific",
        "available_strategies": [
            {"strategy": r["strategy"], "rate": r["recovery_rate"],
             "n": r["attempts"], "sufficient": r["sufficient"]}
            for r in merchant_rows
        ],
        "best": merchant_best["strategy"] if merchant_best else None,
        "used": False,  # will be updated below
    })

    if merchant_best:
        default_row = next(
            (r for r in merchant_rows
             if r["strategy"] == rule_default and r.get("sufficient")), None
        )
        default_rate = default_row["recovery_rate"] if default_row else 0.0
        improvement = (merchant_best["recovery_rate"] - default_rate) * 100
        if merchant_best["strategy"] != rule_default and improvement < MIN_IMPROVEMENT_PP:
            # Not enough improvement to justify switching
            merchant_best = default_row or merchant_best
        explain[-1]["used"] = True
        return _result(
            strategy=merchant_best["strategy"],
            level="merchant_specific",
            sample_size=merchant_best["attempts"],
            recovery_rate=merchant_best["recovery_rate"],
            fallback_used=False,
            explain=explain,
            provenance=require_provenance,
        )

    # --- Level 2: failure_reason ---
    reason_rows = _get_rates_for_dimension(
        conn, "failure_reason", fail_reason, provenance=require_provenance
    )
    reason_best = _best_from_rows(reason_rows)
    explain.append({
        "level": "failure_reason",
        "available_strategies": [
            {"strategy": r["strategy"], "rate": r["recovery_rate"],
             "n": r["attempts"], "sufficient": r["sufficient"]}
            for r in reason_rows
        ],
        "best": reason_best["strategy"] if reason_best else None,
        "used": False,
    })

    if reason_best:
        explain[-1]["used"] = True
        return _result(
            strategy=reason_best["strategy"],
            level="failure_reason",
            sample_size=reason_best["attempts"],
            recovery_rate=reason_best["recovery_rate"],
            fallback_used=True,
            explain=explain,
            provenance=require_provenance,
        )

    # --- Level 3: global ---
    global_rows = _get_rates_for_dimension(
        conn, "global", "all", provenance=require_provenance
    )
    global_best = _best_from_rows(global_rows)
    explain.append({
        "level": "global",
        "available_strategies": [
            {"strategy": r["strategy"], "rate": r["recovery_rate"],
             "n": r["attempts"], "sufficient": r["sufficient"]}
            for r in global_rows
        ],
        "best": global_best["strategy"] if global_best else None,
        "used": False,
    })

    if global_best:
        explain[-1]["used"] = True
        return _result(
            strategy=global_best["strategy"],
            level="global",
            sample_size=global_best["attempts"],
            recovery_rate=global_best["recovery_rate"],
            fallback_used=True,
            explain=explain,
            provenance=require_provenance,
        )

    # --- Level 4: rule-based default (no observed data) ---
    explain.append({"level": "rule_based", "used": True, "strategy": rule_default})
    return _result(
        strategy=rule_default,
        level="rule_based",
        sample_size=0,
        recovery_rate=None,
        fallback_used=True,
        explain=explain,
        provenance=require_provenance,
        data_type="estimate",
    )


def _result(
    strategy: str,
    level: str,
    sample_size: int,
    recovery_rate: Optional[float],
    fallback_used: bool,
    explain: list,
    provenance: Optional[str],
    data_type: str = "actual",
) -> dict:
    return {
        "recommended_strategy": strategy,
        "confidence_level": level,
        "sample_size": sample_size,
        "recovery_rate": recovery_rate,
        "recovery_rate_pct": f"{recovery_rate * 100:.1f}%" if recovery_rate is not None else "N/A",
        "fallback_used": fallback_used,
        "explain": explain,
        "data_type": data_type,
        "provenance_filter": provenance,
        "insufficient_data": sample_size < MIN_SAMPLE,
    }


def strategy_ranking(
    conn,
    dimension_key: str,
    dimension_value: str,
    provenance: Optional[str] = None,
) -> list[dict]:
    """Rank all strategies observed for a given dimension, best first.

    Returns a list suitable for a UI comparison table. Rows with insufficient
    data carry a flag but are still returned (so the UI can show sample size
    and explain why no recommendation is given).
    """
    rows = _get_rates_for_dimension(conn, dimension_key, dimension_value, provenance)
    rows.sort(key=lambda r: (r["sufficient"], r["recovery_rate"]), reverse=True)
    return rows


def full_learning_summary(conn) -> dict:
    """Return a summary of all learned strategy performance, all dimensions.

    Groups by (dimension_key, dimension_value) and returns the strategy ranking
    for each, clearly labelled with data type and sample sizes.
    """
    all_rows = db.get_strategy_performance(conn)

    # Build dimension index
    dims: dict[tuple, list] = {}
    for row in all_rows:
        key = (row["dimension_key"], row["dimension_value"])
        row["recovery_rate"] = round(_recovery_rate(row), 4)
        row["sufficient"] = row["attempts"] >= MIN_SAMPLE
        dims.setdefault(key, []).append(row)

    sections = []
    for (dim_key, dim_val), rows in sorted(dims.items()):
        rows.sort(key=lambda r: (r["sufficient"], r["recovery_rate"]), reverse=True)
        best = next((r for r in rows if r["sufficient"]), None)
        sections.append({
            "dimension_key": dim_key,
            "dimension_value": dim_val,
            "strategies": rows,
            "best_strategy": best["strategy"] if best else None,
            "best_recovery_rate": best["recovery_rate"] if best else None,
            "best_sample_size": best["attempts"] if best else 0,
            "has_sufficient_data": best is not None,
            "provenance_mix": list({r["provenance"] for r in rows}),
        })

    # Provenance summary
    prov_counts: dict[str, int] = {}
    for row in all_rows:
        if row["dimension_key"] == "global":
            prov = row["provenance"]
            prov_counts[prov] = prov_counts.get(prov, 0) + row["attempts"]

    real_test_total = prov_counts.get(PROV_REAL_TEST, 0)

    return {
        "data_type": "actual" if real_test_total > 0 else "historical_or_simulation",
        "provenance_summary": prov_counts,
        "real_test_observations": real_test_total,
        "real_test_note": (
            f"{real_test_total} observations from real Razorpay Test Mode. "
            "These are the most reliable evidence for strategy performance."
        ) if real_test_total > 0 else (
            "No real Razorpay Test Mode observations yet. "
            "All performance data is from simulation or historical pipeline runs. "
            "Run real experiments to collect authentic outcome data."
        ),
        "dimensions": sections,
        "min_sample_for_recommendation": MIN_SAMPLE,
    }
