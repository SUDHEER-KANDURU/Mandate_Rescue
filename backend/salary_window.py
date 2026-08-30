"""Salary-window inference (design.md section 5, R8).

Generic fallback windows for everyone; per-customer inference when there is enough
history. History is stored on the case as `history_success_days` (comma-separated
day-of-month integers of prior successful payments).
"""

from collections import Counter

# Generic retry windows: around month start (1-3) and month end (25-31).
GENERIC_WINDOWS = [(1, 3), (25, 31)]
MIN_HISTORY_POINTS = 3


def _parse_history(case):
    raw = case.get("history_success_days")
    if not raw:
        return []
    days = []
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit():
            days.append(int(part))
    return days


def infer_window(case):
    """Return a dict describing the chosen salary window (inferred or generic)."""
    days = _parse_history(case)
    if len(days) >= MIN_HISTORY_POINTS:
        modal_day = Counter(days).most_common(1)[0][0]
        low = max(1, modal_day - 1)
        high = min(31, modal_day + 1)
        reason = f"inferred salary window around day {modal_day} from {len(days)} past successful payments"
        return {"target_day": modal_day, "window": (low, high), "inferred": True, "label": "inferred (v2 personalization)", "reason": reason}
    generic = GENERIC_WINDOWS[0]
    reason = f"generic salary window (days {generic[0]}-{generic[1]}); insufficient history ({len(days)} points) to personalize"
    return {"target_day": generic[0], "window": generic, "inferred": False, "label": "generic", "reason": reason}
