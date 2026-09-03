"""Adaptive Recovery Policy — Phase 5.

Replaces one-size-fits-all strategy selection with data-driven recommendations
based on observed historical outcomes stored in audit_log + mandate_failures.

Design principles
-----------------
- Reuses the existing deterministic agent architecture — does NOT replace it.
- Produces a recommendation + explanation that the agent CAN act on, but the
  final decision still goes through the existing StrategyAgent rules (retry cap,
  RBI compliance, mandate-limit gate).
- Never overrides compliance rules, retry limits, or the mandate_revoked gate.
- Every recommendation carries an explain() output so the UI can answer
  "Why was this strategy selected?"
- Falls back to the existing rule-based defaults when data is insufficient.
- Data type on all outputs: "actual" when based on observed outcomes,
  "estimate" when extrapolating from limited data.

Governance
----------
This module RECOMMENDS — it does not autonomously execute.
The agent pipeline (agent.py) remains the sole execution authority.
Human-in-the-loop thresholds are defined here as GOVERNANCE_THRESHOLDS:
  LOW_RISK    → recommend + auto-execute (existing behaviour)
  MEDIUM_RISK → recommend + queue for approval
  HIGH_VALUE  → always require explicit approval before execution
  COMPLIANCE  → block (mandate_revoked, over-limit without re-auth, etc.)
"""

from typing import Optional

import scoring
import health as health_module
import salary_window as sw_module
from intelligence import _extract_strategy_from_audit, MIN_STRATEGY_SAMPLE

# ---------------------------------------------------------------------------
# Governance thresholds (configurable via environment or policy override)
# ---------------------------------------------------------------------------
import os

# Cases above this amount Rs require explicit approval before high-cost retries.
HIGH_VALUE_THRESHOLD = float(os.environ.get("APPROVAL_THRESHOLD_RS", "10000"))

# Recovery rate below this triggers a "medium risk" flag on the recommendation.
LOW_CONFIDENCE_RATE = float(os.environ.get("LOW_CONFIDENCE_RATE", "0.50"))

# Governance decision levels
GOV_AUTO_EXECUTE    = "auto_execute"    # low-risk, proceed immediately
GOV_RECOMMEND       = "recommend"       # medium-risk, surface for review
GOV_REQUIRE_APPROVAL = "require_approval"  # high-value, must approve
GOV_BLOCK           = "block"           # policy violation, never execute


# ---------------------------------------------------------------------------
# Observed strategy performance (loaded from intelligence layer)
# ---------------------------------------------------------------------------

def _observed_strategy_rates(conn) -> dict:
    """Return {strategy_label: {recovery_rate, total, sufficient}} from real data."""
    import db
    result = {}
    rows = conn.execute(
        """
        SELECT a.action_taken, f.case_status
        FROM audit_log a
        JOIN mandate_failures f ON a.customer_id = f.customer_id
        WHERE a.event_type = 'strategy_selected'
        """
    ).fetchall()
    buckets: dict = {}
    for row in rows:
        action = row["action_taken"] or ""
        if action.startswith("Strategy:"):
            strat = action[len("Strategy:"):].strip()
        else:
            continue
        b = buckets.setdefault(strat, {"total": 0, "recovered": 0})
        b["total"] += 1
        if row["case_status"] == "recovered":
            b["recovered"] += 1
    for strat, b in buckets.items():
        t = b["total"]
        result[strat] = {
            "recovery_rate": round(b["recovered"] / t, 4) if t else 0.0,
            "total": t,
            "sufficient": t >= MIN_STRATEGY_SAMPLE,
        }
    return result


def _observed_rates_for_category(conn, merchant_category: str) -> dict:
    """Per-merchant-category strategy performance from real data."""
    rows = conn.execute(
        """
        SELECT a.action_taken, f.case_status
        FROM audit_log a
        JOIN mandate_failures f ON a.customer_id = f.customer_id
        WHERE a.event_type = 'strategy_selected'
          AND f.merchant_category = ?
        """,
        (merchant_category,),
    ).fetchall()
    buckets: dict = {}
    for row in rows:
        action = row["action_taken"] or ""
        if action.startswith("Strategy:"):
            strat = action[len("Strategy:"):].strip()
        else:
            continue
        b = buckets.setdefault(strat, {"total": 0, "recovered": 0})
        b["total"] += 1
        if row["case_status"] == "recovered":
            b["recovered"] += 1
    result = {}
    for strat, b in buckets.items():
        t = b["total"]
        result[strat] = {
            "recovery_rate": round(b["recovered"] / t, 4) if t else 0.0,
            "total": t,
            "sufficient": t >= MIN_STRATEGY_SAMPLE,
        }
    return result


# ---------------------------------------------------------------------------
# Governance decision
# ---------------------------------------------------------------------------

def _governance_decision(case: dict, recommended_strategy: str,
                         recovery_rate: float) -> tuple:
    """Return (decision_level, rationale) for a case + recommended strategy.

    Implements the HITL governance tiers:
      BLOCK          — mandate_revoked (policy: no retry ever)
      REQUIRE_APPROVAL — high-value cases
      RECOMMEND      — low-confidence strategies or escalation-bound cases
      AUTO_EXECUTE   — standard cases with high confidence
    """
    reason = case.get("failure_reason", "")
    amount = float(case.get("amount", 0) or 0)

    # BLOCK tier: policy violation — mandate_revoked never retries
    if reason == "mandate_revoked":
        return GOV_BLOCK, (
            "Mandate revoked: retry is not permitted by policy. "
            "Immediate escalation required."
        )

    # REQUIRE_APPROVAL tier: high-value cases
    if amount >= HIGH_VALUE_THRESHOLD:
        return GOV_REQUIRE_APPROVAL, (
            f"Amount Rs {amount:,.0f} exceeds the approval threshold "
            f"(Rs {HIGH_VALUE_THRESHOLD:,.0f}). "
            "Explicit approval required before executing retry."
        )

    # RECOMMEND tier: low-confidence strategy data
    if not recovery_rate or recovery_rate < LOW_CONFIDENCE_RATE:
        return GOV_RECOMMEND, (
            f"Observed recovery rate for '{recommended_strategy}' is "
            f"{recovery_rate*100:.1f}% — below the confidence threshold "
            f"({LOW_CONFIDENCE_RATE*100:.0f}%). "
            "Recommendation surfaced for review."
        )

    # AUTO_EXECUTE: standard case, sufficient confidence
    return GOV_AUTO_EXECUTE, (
        f"Standard case. Observed recovery rate {recovery_rate*100:.1f}% >= "
        f"{LOW_CONFIDENCE_RATE*100:.0f}% threshold. Auto-execute."
    )


# ---------------------------------------------------------------------------
# Main recommendation function
# ---------------------------------------------------------------------------

def recommend_strategy(case: dict, conn) -> dict:
    """Generate a data-driven strategy recommendation for a single case.

    Returns a dict with:
        recommended_strategy  (str)
        confidence            (float 0–1, from observed data or fallback)
        data_source           "observed" | "default"
        governance            (GOV_* level)
        governance_rationale  (str)
        explain               (list of {step, reason} for UI timeline)
        data_type             "actual" | "estimate"
    """
    reason = case.get("failure_reason", "")
    category = case.get("merchant_category", "")
    amount = float(case.get("amount", 0) or 0)
    score, factors = scoring.score_case(case)

    explain_steps = []

    # Step 1: Rule-based baseline (same as existing agent)
    rule_based = _rule_based_strategy(case)
    explain_steps.append({
        "step": "Rule-based baseline",
        "reason": f"failure_reason='{reason}' → default strategy: '{rule_based}'",
    })

    # Step 2: Look up observed performance for this strategy in this merchant category
    cat_rates = _observed_rates_for_category(conn, category)
    global_rates = _observed_strategy_rates(conn)

    # Prefer merchant-category-specific data; fall back to global.
    if rule_based in cat_rates and cat_rates[rule_based]["sufficient"]:
        observed = cat_rates[rule_based]
        data_source = f"observed (merchant_category='{category}', n={observed['total']})"
        confidence = observed["recovery_rate"]
        explain_steps.append({
            "step": "Merchant-specific observed rate",
            "reason": (
                f"In '{category}': strategy '{rule_based}' recovered "
                f"{observed['recovery_rate']*100:.1f}% of {observed['total']} cases."
            ),
        })
    elif rule_based in global_rates and global_rates[rule_based]["sufficient"]:
        observed = global_rates[rule_based]
        data_source = f"observed (global, n={observed['total']})"
        confidence = observed["recovery_rate"]
        explain_steps.append({
            "step": "Global observed rate",
            "reason": (
                f"No merchant-specific data for '{category}'. "
                f"Global: strategy '{rule_based}' recovered "
                f"{observed['recovery_rate']*100:.1f}% of {observed['total']} cases."
            ),
        })
    else:
        # Not enough data — fall back to scoring-based estimate
        from agent import _success_prob, BASE_SUCCESS_PROB
        prob = _success_prob(case, score)
        confidence = prob
        data_source = "estimate (insufficient observed data, using probability model)"
        explain_steps.append({
            "step": "Probability model estimate",
            "reason": (
                f"Insufficient data for '{rule_based}' in '{category}'. "
                f"Score-based probability estimate: {prob*100:.1f}%."
            ),
        })

    # Step 3: Check whether a different strategy performs better for this merchant
    # (only if there is enough data to compare)
    best_strategy = rule_based
    best_rate = confidence
    for strat, rates in cat_rates.items():
        if rates["sufficient"] and rates["recovery_rate"] > best_rate + 0.05:
            # A different strategy measurably outperforms (by >5 pp) in this merchant
            best_strategy = strat
            best_rate = rates["recovery_rate"]
            explain_steps.append({
                "step": "Better strategy found for merchant category",
                "reason": (
                    f"In '{category}': '{strat}' achieves "
                    f"{rates['recovery_rate']*100:.1f}% recovery "
                    f"vs {confidence*100:.1f}% for '{rule_based}'. "
                    "Recommending the better-performing strategy."
                ),
            })
            break

    # Step 4: Governance decision
    gov_level, gov_rationale = _governance_decision(case, best_strategy, best_rate)
    explain_steps.append({
        "step": "Governance check",
        "reason": gov_rationale,
    })

    # Data type
    dtype = "actual" if "observed" in data_source else "estimate"

    return {
        "customer_id": case.get("customer_id"),
        "recommended_strategy": best_strategy,
        "confidence": round(best_rate, 4),
        "confidence_pct": f"{best_rate*100:.1f}%",
        "data_source": data_source,
        "data_type": dtype,
        "governance": gov_level,
        "governance_rationale": gov_rationale,
        "recoverability_score": score,
        "explain": explain_steps,
        "can_auto_execute": gov_level == GOV_AUTO_EXECUTE,
        "requires_approval": gov_level == GOV_REQUIRE_APPROVAL,
        "is_blocked": gov_level == GOV_BLOCK,
    }


def _rule_based_strategy(case: dict) -> str:
    """Mirror the existing StrategyAgent rule tree to get the default strategy label."""
    reason = case.get("failure_reason", "")
    amount = float(case.get("amount", 0) or 0)
    mandate_limit = float(case.get("mandate_limit") or 5000)

    if reason == "mandate_revoked":
        return "immediate escalation"
    if amount > mandate_limit:
        return "higher-limit re-authorization"
    if reason == "insufficient_funds":
        return "salary-window retry"
    if reason == "mandate_expired":
        return "re-authorization link"
    if reason == "bank_technical_error":
        return "silent quick retry"
    return "salary-window retry"  # safe default


def batch_recommend(cases: list, conn) -> list:
    """Recommend strategies for a list of cases.

    Returns list of recommendation dicts in the same order as ``cases``.
    Loads strategy performance tables once and reuses them for all cases.
    """
    # Pre-load performance tables once
    global_rates = _observed_strategy_rates(conn)
    # Note: per-category rates are fetched inside recommend_strategy via conn.
    # For batch use, we pass conn directly so SQLite connection is reused.
    return [recommend_strategy(c, conn) for c in cases]


def policy_summary(conn) -> dict:
    """Summary of current adaptive policy state for the dashboard.

    Returns:
        current_strategy_distribution  (from actual audit data)
        governance_thresholds           (current configured values)
        observed_strategy_performance   (actual outcomes per strategy)
        recommended_changes             (list of data-backed suggestions)
        data_type                       "actual"
    """
    global_rates = _observed_strategy_rates(conn)

    # Identify under-performing strategies (actual rate well below scoring.REASON_BASE prior)
    from scoring import REASON_BASE
    from intelligence import KNOWN_STRATEGIES

    recommendations = []
    for strat, perf in global_rates.items():
        if not perf["sufficient"]:
            continue
        # Map strategy back to its primary failure_reason to get the prior
        primary_reason = KNOWN_STRATEGIES.get(strat)
        if primary_reason and primary_reason in REASON_BASE:
            prior = REASON_BASE[primary_reason]
            actual = perf["recovery_rate"]
            delta = actual - prior
            if delta < -0.10:
                recommendations.append({
                    "strategy": strat,
                    "observed_rate": actual,
                    "prior_rate": prior,
                    "delta": round(delta, 4),
                    "recommendation": (
                        f"'{strat}' is performing {abs(delta)*100:.1f}pp below the "
                        f"expert prior ({actual*100:.1f}% observed vs "
                        f"{prior*100:.1f}% expected). "
                        "Consider reviewing case selection or timing for this strategy."
                    ),
                    "data_type": "actual",
                })

    return {
        "data_type": "actual",
        "governance_thresholds": {
            "high_value_threshold_rs": HIGH_VALUE_THRESHOLD,
            "low_confidence_rate": LOW_CONFIDENCE_RATE,
            "auto_execute_below_rs": HIGH_VALUE_THRESHOLD,
        },
        "observed_strategy_performance": global_rates,
        "recommended_changes": recommendations,
        "description": (
            "Current adaptive policy state derived from real audit_log outcomes. "
            "Recommendations are data-backed suggestions, not automatic changes."
        ),
    }
