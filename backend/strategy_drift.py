"""Strategy Drift Detection — Phase 6.

Detects when a previously strong strategy's performance starts degrading.

Method
------
Compares performance in a RECENT window (last N cases) vs the BASELINE
(all prior cases). When the recent recovery rate drops significantly below
the baseline, a drift alert is raised.

We use relative-change thresholds (not z-scores) because the strategy_performance
table accumulates over time and we cannot reconstruct time-series from it directly.
Instead we use the audit_log + mandate_failures tables to build rolling windows:

  baseline_window: all resolved cases before the recent_cutoff
  recent_window:   resolved cases in the last RECENT_WINDOW_DAYS days

For each (strategy, dimension) pair with sufficient samples in BOTH windows:
  drift_score = (baseline_rate - recent_rate) / baseline_rate
  if drift_score > DRIFT_THRESHOLD → raise alert

This is clearly labelled "actual" since it reads real case outcomes.

Public API
----------
detect_strategy_drift(conn) → dict
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import db
from intelligence import _extract_strategy_from_audit, MIN_STRATEGY_SAMPLE

# Days to look back for the "recent" window.
RECENT_WINDOW_DAYS = int(os.environ.get("DRIFT_WINDOW_DAYS", "30"))

# Relative drop required to flag drift: 0.15 = 15% relative drop.
DRIFT_THRESHOLD = float(os.environ.get("DRIFT_THRESHOLD", "0.15"))

# Minimum cases in EACH window to compare.
MIN_WINDOW_SAMPLE = int(os.environ.get("DRIFT_MIN_SAMPLE", "5"))

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING  = "warning"
SEVERITY_INFO     = "info"


def _recent_cutoff() -> str:
    """ISO-8601 timestamp for RECENT_WINDOW_DAYS ago."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_WINDOW_DAYS)
    return cutoff.isoformat(timespec="seconds")


def _cases_by_strategy(conn, after_date: Optional[str] = None,
                        before_date: Optional[str] = None) -> dict:
    """Return {strategy: {total, recovered, amount_recovered}} for a date range.

    Filters on failure_date (the date the original failure occurred, which is
    the closest proxy we have for "when was this case processed" in historical data).
    """
    cases = db.get_all_cases(conn)

    # Build audit index
    audit_by_case: dict = {}
    for row in conn.execute(
        "SELECT customer_id, event_type, action_taken FROM audit_log ORDER BY event_id"
    ).fetchall():
        audit_by_case.setdefault(row["customer_id"], []).append(dict(row))

    from outcome_attribution import TERMINAL_STATUSES

    buckets: dict = {}
    for c in cases:
        if c.get("case_status") not in TERMINAL_STATUSES:
            continue
        fd = c.get("failure_date", "")
        if after_date and fd < after_date:
            continue
        if before_date and fd >= before_date:
            continue

        strategy = _extract_strategy_from_audit(
            audit_by_case.get(c["customer_id"], [])
        )
        if not strategy:
            continue

        b = buckets.setdefault(strategy, {"total": 0, "recovered": 0,
                                           "amount_recovered": 0.0})
        b["total"] += 1
        if c["case_status"] == "recovered":
            b["recovered"] += 1
            b["amount_recovered"] += float(c.get("amount", 0) or 0)

    return buckets


def detect_strategy_drift(conn) -> dict:
    """Detect strategies showing significant performance degradation.

    Returns:
        data_type: "actual"
        recent_window_days: int
        drift_threshold_pct: float
        alerts: list of drift alert dicts
        no_drift_strategies: list of strategies checked without drift
        insufficient_data_strategies: strategies with too few samples in a window
    """
    cutoff = _recent_cutoff()
    recent = _cases_by_strategy(conn, after_date=cutoff)
    baseline = _cases_by_strategy(conn, before_date=cutoff)

    alerts = []
    no_drift = []
    insufficient = []

    all_strategies = set(recent) | set(baseline)

    for strategy in sorted(all_strategies):
        b_row = baseline.get(strategy, {"total": 0, "recovered": 0})
        r_row = recent.get(strategy, {"total": 0, "recovered": 0})

        b_n = b_row["total"]
        r_n = r_row["total"]

        if b_n < MIN_WINDOW_SAMPLE or r_n < MIN_WINDOW_SAMPLE:
            insufficient.append({
                "strategy": strategy,
                "baseline_n": b_n,
                "recent_n": r_n,
                "reason": (
                    f"Baseline window: {b_n} cases (need ≥ {MIN_WINDOW_SAMPLE}), "
                    f"recent window: {r_n} cases (need ≥ {MIN_WINDOW_SAMPLE})."
                ),
            })
            continue

        b_rate = b_row["recovered"] / b_n
        r_rate = r_row["recovered"] / r_n

        if b_rate == 0:
            no_drift.append({"strategy": strategy, "note": "baseline_rate_zero"})
            continue

        relative_drop = (b_rate - r_rate) / b_rate

        if relative_drop > DRIFT_THRESHOLD:
            severity = SEVERITY_CRITICAL if relative_drop > 0.30 else SEVERITY_WARNING
            alerts.append({
                "strategy": strategy,
                "severity": severity,
                "alert_type": "strategy_performance_degradation",
                "title": f"Strategy drift: '{strategy}'",
                "description": (
                    f"'{strategy}' recovery rate has dropped from "
                    f"{b_rate*100:.1f}% (baseline, {b_n} cases) to "
                    f"{r_rate*100:.1f}% (last {RECENT_WINDOW_DAYS} days, {r_n} cases). "
                    f"Relative drop: {relative_drop*100:.1f}%."
                ),
                "baseline_rate": round(b_rate, 4),
                "baseline_n": b_n,
                "recent_rate": round(r_rate, 4),
                "recent_n": r_n,
                "relative_drop": round(relative_drop, 4),
                "recommended_action": (
                    f"Investigate recent '{strategy}' cases. Possible causes: "
                    "change in failure-type distribution, payment-method mix shift, "
                    "merchant-side policy change, retry timing drift, "
                    "or seasonal behaviour."
                ),
                "data_type": "actual",
            })
        else:
            no_drift.append({
                "strategy": strategy,
                "baseline_rate": round(b_rate, 4),
                "recent_rate": round(r_rate, 4),
                "relative_drop": round(relative_drop, 4),
                "stable": True,
            })

    # Sort critical first
    _sev_order = {SEVERITY_CRITICAL: 0, SEVERITY_WARNING: 1, SEVERITY_INFO: 2}
    alerts.sort(key=lambda a: _sev_order.get(a["severity"], 9))

    return {
        "data_type": "actual",
        "recent_window_days": RECENT_WINDOW_DAYS,
        "recent_cutoff_date": cutoff,
        "drift_threshold_pct": round(DRIFT_THRESHOLD * 100, 1),
        "min_window_sample": MIN_WINDOW_SAMPLE,
        "alerts": alerts,
        "has_drift": len(alerts) > 0,
        "no_drift_strategies": no_drift,
        "insufficient_data_strategies": insufficient,
        "description": (
            f"Strategy drift is detected when the recovery rate in the last "
            f"{RECENT_WINDOW_DAYS} days drops more than {DRIFT_THRESHOLD*100:.0f}% "
            f"relative to the baseline (all prior data). "
            f"Minimum {MIN_WINDOW_SAMPLE} cases required in each window."
        ),
    }
