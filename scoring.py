"""Recoverability scoring (design.md section 4).

A transparent weighted 0-100 score. Higher means more worth pursuing. Weights and
the per-reason base table are named constants so they are easy to audit and tune.
"""

# Weights sum to 1.0
W_SUCCESS = 0.40
W_TENURE = 0.20
W_RETRY = 0.20
W_REASON = 0.20

TENURE_CAP_MONTHS = 24
RETRY_CAP = 3

# Per-reason base recoverability (0-1)
REASON_BASE = {
    "bank_technical_error": 0.95,  # usually transient
    "insufficient_funds": 0.70,    # recoverable with timing
    "mandate_expired": 0.55,       # needs customer re-auth action
    "mandate_revoked": 0.10,       # rarely recoverable
}


def score_case(case):
    """Return (score:int 0-100, factors:dict) for a mandate_failures dict."""
    success = max(0.0, min(float(case.get("past_payment_success_rate", 0.0)), 1.0))
    tenure_component = min(float(case.get("customer_tenure_months", 0)) / TENURE_CAP_MONTHS, 1.0)
    retry_component = 1.0 - min(float(case.get("past_retry_count", 0)) / RETRY_CAP, 1.0)
    reason_component = REASON_BASE.get(case.get("failure_reason", ""), 0.5)
    raw = (W_SUCCESS * success + W_TENURE * tenure_component
           + W_RETRY * retry_component + W_REASON * reason_component)
    factors = {
        "success_rate": round(success, 2),
        "tenure_component": round(tenure_component, 2),
        "retry_component": round(retry_component, 2),
        "reason_base": reason_component,
    }
    return round(100 * raw), factors


def explain_score(case, score, factors):
    """Plain-English reasoning string for the score event (R7)."""
    return (
        f"Recoverability {score}/100: {int(factors['success_rate'] * 100)}% historical "
        f"success rate, {case.get('customer_tenure_months', 0)} months tenure, "
        f"{case.get('past_retry_count', 0)} prior retries, and a "
        f"'{case.get('failure_reason', '')}' failure (base recoverability "
        f"{factors['reason_base']})."
    )
