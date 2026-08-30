"""Subscription health score (design.md R17, STRETCH).

A lightweight per-customer churn-risk indicator that complements the per-case
recoverability score (R6). Where the recoverability score answers "is THIS failure
worth pursuing?", the health score answers "how healthy is this customer's
subscription overall?" -- combining their historical payment success rate with
their accumulated retry burden.

This reflects Razorpay's positioning of Subscriptions as a single control hub for
overall subscription health, not just individual failures.
"""

# Weights for the two signals (sum to 1.0).
W_SUCCESS = 0.70   # historical payment reliability dominates
W_RETRY = 0.30     # accumulated retry burden erodes health

RETRY_CAP = 3      # normalize retry burden against the hard retry cap


def health_score(success_rate, retry_count):
    """Return a 0-100 subscription health score. Higher = healthier."""
    success = max(0.0, min(float(success_rate), 1.0))
    retry_penalty = min(float(retry_count) / RETRY_CAP, 1.0)
    raw = W_SUCCESS * success + W_RETRY * (1.0 - retry_penalty)
    return round(100 * raw)


def health_band(score):
    """Bucket a score into a churn-risk band for the UI."""
    if score >= 70:
        return "healthy"
    if score >= 45:
        return "at-risk"
    return "high-risk"


def health_for_case(case):
    """Compute health score + band + reasoning for a mandate_failures dict."""
    score = health_score(case.get("past_payment_success_rate", 0.0),
                          case.get("past_retry_count", 0))
    band = health_band(score)
    reasoning = (
        f"Subscription health {score}/100 ({band}): "
        f"{int(float(case.get('past_payment_success_rate', 0)) * 100)}% historical "
        f"payment success and {case.get('past_retry_count', 0)} accumulated retries."
    )
    return {"health_score": score, "health_band": band, "reasoning": reasoning}
