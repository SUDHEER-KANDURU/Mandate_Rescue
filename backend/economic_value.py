"""Economic Decision Engine — Phase 5.

Computes expected net value of recovery interventions so the system optimises
for *value* rather than just *probability of recovery*.

The core idea:
    E[net_value] = P(recovery) × amount_recovered
                  - intervention_cost
                  - customer_friction_cost

Where:
  P(recovery)         = from scoring._success_prob (same model as agent)
  amount_recovered    = case.amount (full amount — no partial recovery modeled)
  intervention_cost   = configurable per-channel (SMS / email / retry API call)
  customer_friction   = penalty for contacting at-risk customers (churn risk proxy)

All cost inputs are configurable; defaults are conservative estimates.
No cost is hardcoded as "the real number" — they are configurable parameters.

Data trust
----------
- P(recovery): "estimate" from the probability model
- amount: "actual" from stored case data
- costs: "configurable" — operator must set realistic values
- net_value: "estimate" (product of estimate × actual - configurable)

Every output carries data_type + cost_assumptions so operators can see exactly
what numbers were used and audit the calculation.

Counterfactual / incremental
-----------------------------
incremental_value(case) computes:
    agent_ev    = E[net_value | agent strategy]
    baseline_ev = E[net_value | 1 naive attempt, no strategy]
    incremental = agent_ev - baseline_ev

This is the defensible measure of what the strategy layer is worth per case.
Clearly labelled "estimate".
"""

import os
from typing import Optional

import scoring
from agent import _success_prob

# ---------------------------------------------------------------------------
# Cost parameters (all in Rs, configurable via environment variables)
# ---------------------------------------------------------------------------

# Cost per retry API call (Razorpay / bank processing)
RETRY_COST_RS = float(os.environ.get("RETRY_COST_RS", "2.00"))

# Cost per SMS dunning message
SMS_COST_RS = float(os.environ.get("SMS_COST_RS", "0.50"))

# Cost per email dunning message
EMAIL_COST_RS = float(os.environ.get("EMAIL_COST_RS", "0.10"))

# Customer friction multiplier: lost LTV fraction per unnecessary contact
# For a 'healthy' customer, aggressively dunning a transient failure costs goodwill.
# Expressed in Rs as a fraction of the payment amount.
FRICTION_RATE_HEALTHY = float(os.environ.get("FRICTION_RATE_HEALTHY", "0.02"))   # 2% of amount
FRICTION_RATE_AT_RISK = float(os.environ.get("FRICTION_RATE_AT_RISK", "0.05"))   # 5% of amount
FRICTION_RATE_HIGH_RISK = float(os.environ.get("FRICTION_RATE_HIGH_RISK", "0.08"))  # 8% of amount

_FRICTION_RATES = {
    "healthy": FRICTION_RATE_HEALTHY,
    "at-risk": FRICTION_RATE_AT_RISK,
    "high-risk": FRICTION_RATE_HIGH_RISK,
}

# Number of dunning messages per strategy type (used for cost calculation)
_STRATEGY_MESSAGES = {
    "salary-window retry": {"sms": 2, "email": 1, "retries": 3},
    "re-authorization link": {"sms": 1, "email": 2, "retries": 2},
    "silent quick retry": {"sms": 0, "email": 0, "retries": 3},
    "immediate escalation": {"sms": 0, "email": 0, "retries": 0},
    "higher-limit re-authorization": {"sms": 1, "email": 2, "retries": 2},
}
_DEFAULT_MESSAGES = {"sms": 1, "email": 1, "retries": 2}


def _strategy_cost(strategy: str) -> dict:
    """Return cost breakdown for a strategy in Rs."""
    msgs = _STRATEGY_MESSAGES.get(strategy, _DEFAULT_MESSAGES)
    sms_cost = msgs["sms"] * SMS_COST_RS
    email_cost = msgs["email"] * EMAIL_COST_RS
    retry_cost = msgs["retries"] * RETRY_COST_RS
    total = round(sms_cost + email_cost + retry_cost, 4)
    return {
        "sms_messages": msgs["sms"],
        "email_messages": msgs["email"],
        "retry_attempts": msgs["retries"],
        "sms_cost_rs": round(sms_cost, 4),
        "email_cost_rs": round(email_cost, 4),
        "retry_cost_rs": round(retry_cost, 4),
        "total_cost_rs": total,
    }


def _friction_cost(case: dict, strategy: str) -> float:
    """Customer friction cost in Rs (proxy for LTV risk from over-contact)."""
    import health as health_module
    h_score = health_module.health_score(
        case.get("past_payment_success_rate", 0.0),
        case.get("past_retry_count", 0),
    )
    h_band = health_module.health_band(h_score)
    rate = _FRICTION_RATES.get(h_band, FRICTION_RATE_AT_RISK)
    # No friction for silent quick retry (no customer contact)
    if strategy == "silent quick retry":
        rate = 0.0
    amount = float(case.get("amount", 0) or 0)
    return round(amount * rate, 4)


def expected_value(case: dict, strategy: str,
                   override_prob: Optional[float] = None) -> dict:
    """Compute expected net value for one case × strategy combination.

    Args:
        case: mandate_failures row dict
        strategy: strategy label (e.g. "salary-window retry")
        override_prob: if provided, use this recovery probability instead of
                       the model estimate (useful for testing with known probs)

    Returns a dict with:
        expected_gross_value    P(recovery) × amount  [estimate]
        intervention_cost       fixed cost of executing the strategy
        friction_cost           estimated LTV penalty for contacting the customer
        expected_net_value      gross - costs  [estimate]
        recovery_probability    the P(recovery) used
        data_type               "estimate"
        cost_assumptions        the cost parameters that were used
    """
    score, _ = scoring.score_case(case)
    prob = override_prob if override_prob is not None else _success_prob(case, score)
    amount = float(case.get("amount", 0) or 0)

    gross = round(prob * amount, 4)
    costs = _strategy_cost(strategy)
    friction = _friction_cost(case, strategy)
    net = round(gross - costs["total_cost_rs"] - friction, 4)

    return {
        "customer_id": case.get("customer_id"),
        "strategy": strategy,
        "amount": amount,
        "recovery_probability": round(prob, 4),
        "expected_gross_value": gross,
        "intervention_cost": costs["total_cost_rs"],
        "friction_cost": friction,
        "expected_net_value": net,
        "cost_breakdown": costs,
        "data_type": "estimate",
        "data_type_note": (
            "recovery_probability is model-estimated; amount is actual; "
            "costs are configurable parameters."
        ),
        "cost_assumptions": {
            "retry_cost_rs": RETRY_COST_RS,
            "sms_cost_rs": SMS_COST_RS,
            "email_cost_rs": EMAIL_COST_RS,
            "friction_rate": _FRICTION_RATES.get(
                __import__("health").health_band(
                    __import__("health").health_score(
                        case.get("past_payment_success_rate", 0.0),
                        case.get("past_retry_count", 0),
                    )
                ),
                FRICTION_RATE_AT_RISK,
            ),
        },
    }


def best_strategy_by_value(case: dict, conn=None) -> dict:
    """Select the strategy with the highest expected net value for a case.

    Evaluates all applicable strategies and returns the one with the best E[net].
    Uses the same governance rules as adaptive_policy.py — blocked strategies
    (mandate_revoked) return net_value=0 and are excluded from selection.

    Returns:
        best_strategy             (str)
        best_expected_net_value   (float, estimate)
        all_strategies            (list of ev dicts for each option)
        data_type                 "estimate"
    """
    from adaptive_policy import _rule_based_strategy, GOV_BLOCK, _governance_decision

    reason = case.get("failure_reason", "")
    amount = float(case.get("amount", 0) or 0)
    mandate_limit = float(case.get("mandate_limit") or 5000)

    # Determine applicable strategies for this case
    applicable = []
    if reason == "mandate_revoked":
        applicable = ["immediate escalation"]
    elif amount > mandate_limit:
        applicable = ["higher-limit re-authorization"]
    elif reason == "insufficient_funds":
        applicable = ["salary-window retry"]
    elif reason == "mandate_expired":
        applicable = ["re-authorization link"]
    elif reason == "bank_technical_error":
        applicable = ["silent quick retry"]
    else:
        applicable = ["salary-window retry"]

    evs = []
    for strat in applicable:
        ev = expected_value(case, strat)
        # Check governance — blocked strategies get net_value forced to -inf
        score_val, _ = scoring.score_case(case)
        gov, _ = _governance_decision(case, strat, ev["recovery_probability"])
        ev["governance"] = gov
        ev["blocked"] = gov == GOV_BLOCK
        if gov == GOV_BLOCK:
            ev["expected_net_value"] = 0.0
        evs.append(ev)

    best = max(evs, key=lambda e: e["expected_net_value"] if not e["blocked"] else -999)

    return {
        "customer_id": case.get("customer_id"),
        "best_strategy": best["strategy"],
        "best_expected_net_value": best["expected_net_value"],
        "best_recovery_probability": best["recovery_probability"],
        "all_strategies": evs,
        "data_type": "estimate",
        "data_type_note": "Expected values are estimates from the probability model.",
    }


def incremental_value(case: dict) -> dict:
    """Compute the incremental expected value of agent strategy vs naive baseline.

    Agent strategy:   appropriate strategy based on failure_reason
    Baseline:         1 naive attempt, no strategy (same probability, higher friction)

    incremental_ev = agent_ev - baseline_ev

    Clearly labelled "estimate" — this is a model-based counterfactual.
    """
    from adaptive_policy import _rule_based_strategy

    agent_strategy = _rule_based_strategy(case)
    naive_strategy = "silent quick retry"  # baseline: 1 attempt, minimal cost

    agent_ev = expected_value(case, agent_strategy)
    baseline_ev = expected_value(case, naive_strategy)

    incremental = round(agent_ev["expected_net_value"] - baseline_ev["expected_net_value"], 4)

    return {
        "customer_id": case.get("customer_id"),
        "amount": float(case.get("amount", 0) or 0),
        "agent_strategy": agent_strategy,
        "agent_expected_net_value": agent_ev["expected_net_value"],
        "baseline_strategy": "naive (1 attempt)",
        "baseline_expected_net_value": baseline_ev["expected_net_value"],
        "incremental_expected_value": incremental,
        "data_type": "estimate",
        "data_type_note": (
            "Incremental value is a model-based counterfactual estimate. "
            "It is NOT the actual realised difference — that can only be measured "
            "from a controlled experiment. [ESTIMATE — counterfactual]"
        ),
    }


def portfolio_ev(conn) -> dict:
    """Compute aggregate expected value metrics across all active cases.

    Returns:
        total_amount_at_risk        (actual)
        total_expected_gross_value  (estimate)
        total_expected_net_value    (estimate)
        total_intervention_cost     (estimate based on configurable parameters)
        total_friction_cost         (estimate)
        by_strategy                 (breakdown per strategy type)
        data_type                   "mixed"
    """
    import db
    from adaptive_policy import _rule_based_strategy

    cases = db.get_all_cases(conn)
    active = [c for c in cases if c["case_status"] not in
              ("recovered", "rejected", "invalid", "duplicate")]

    totals = {
        "amount_at_risk": 0.0,
        "expected_gross": 0.0,
        "expected_net": 0.0,
        "intervention_cost": 0.0,
        "friction_cost": 0.0,
    }
    by_strat: dict = {}

    for case in active:
        strat = _rule_based_strategy(case)
        ev = expected_value(case, strat)
        totals["amount_at_risk"] += ev["amount"]
        totals["expected_gross"] += ev["expected_gross_value"]
        totals["expected_net"] += ev["expected_net_value"]
        totals["intervention_cost"] += ev["intervention_cost"]
        totals["friction_cost"] += ev["friction_cost"]

        b = by_strat.setdefault(strat, {
            "strategy": strat,
            "case_count": 0,
            "amount_at_risk": 0.0,
            "expected_net_value": 0.0,
        })
        b["case_count"] += 1
        b["amount_at_risk"] += ev["amount"]
        b["expected_net_value"] += ev["expected_net_value"]

    for b in by_strat.values():
        b["amount_at_risk"] = round(b["amount_at_risk"], 2)
        b["expected_net_value"] = round(b["expected_net_value"], 2)

    return {
        "data_type": "mixed",
        "data_type_note": (
            "amount_at_risk is actual. All other values are estimates derived "
            "from the probability model and configurable cost parameters."
        ),
        "active_cases": len(active),
        "total_amount_at_risk": round(totals["amount_at_risk"], 2),
        "total_expected_gross_value": round(totals["expected_gross"], 2),
        "total_expected_net_value": round(totals["expected_net"], 2),
        "total_intervention_cost": round(totals["intervention_cost"], 2),
        "total_friction_cost": round(totals["friction_cost"], 2),
        "by_strategy": sorted(
            by_strat.values(), key=lambda b: b["expected_net_value"], reverse=True
        ),
        "cost_assumptions": {
            "retry_cost_rs": RETRY_COST_RS,
            "sms_cost_rs": SMS_COST_RS,
            "email_cost_rs": EMAIL_COST_RS,
        },
    }
