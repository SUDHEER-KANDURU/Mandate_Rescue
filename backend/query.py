"""Structured query execution for the natural-language ask feature.

The LLM turns a question into a small filter spec (see llm_client.translate_query).
This module executes that spec against REAL data: stored-column filters run as
parameterized SQL against mandate_failures; computed filters (recoverability score,
health band) are applied in Python using the same scoring.py / health.py functions
that power the rest of the dashboard. No case data is ever generated here.
"""

import db
import scoring
import health as health_module

# Columns we accept as direct equality filters, mapped to their SQL column.
_EQ_COLUMNS = {
    "failure_reason": "failure_reason",
    "merchant_category": "merchant_category",
    "compliance_status": "compliance_status",
    "case_status": "case_status",
}

_VALID_VALUES = {
    "failure_reason": {"insufficient_funds", "mandate_expired", "mandate_revoked",
                       "bank_technical_error"},
    "merchant_category": {"subscription", "emi", "insurance", "utility"},
    "compliance_status": {"RBI-compliant", "non-compliant"},
    "case_status": {"new", "in_progress", "recovered", "escalated", "promised",
                    "broken_promise"},
    "health_band": {"healthy", "at-risk", "high-risk"},
}


def _to_number(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _row_score(case):
    return scoring.score_case(case)[0]


def _row_health_band(case):
    hs = health_module.health_score(case.get("past_payment_success_rate", 0.0),
                                    case.get("past_retry_count", 0))
    return health_module.health_band(hs)


def run_query(spec, limit=100):
    """Execute a validated filter spec. Returns (rows:list[dict], applied:dict).

    `applied` echoes the filters that were actually used (after validation), so the
    UI can show the user exactly how their question was interpreted.
    """
    spec = spec or {}
    where = []
    params = []
    applied = {}

    # --- parameterized SQL for stored columns -------------------------------
    for key, col in _EQ_COLUMNS.items():
        if key in spec:
            val = str(spec[key])
            if val in _VALID_VALUES.get(key, set()):
                where.append(f"{col} = ?")
                params.append(val)
                applied[key] = val

    amount_min = _to_number(spec.get("amount_min"))
    if amount_min is not None:
        where.append("amount >= ?")
        params.append(amount_min)
        applied["amount_min"] = amount_min
    amount_max = _to_number(spec.get("amount_max"))
    if amount_max is not None:
        where.append("amount <= ?")
        params.append(amount_max)
        applied["amount_max"] = amount_max

    dsm = _to_number(spec.get("dunning_stage_min"))
    if dsm is not None:
        where.append("dunning_stage >= ?")
        params.append(int(dsm))
        applied["dunning_stage_min"] = int(dsm)

    over_limit = spec.get("over_limit")
    if isinstance(over_limit, bool) or str(over_limit).lower() in ("true", "false"):
        is_over = over_limit is True or str(over_limit).lower() == "true"
        # mandate_limit defaults to 5000 when null (mirrors the agent's gate).
        where.append("amount " + (">" if is_over else "<=") + " COALESCE(mandate_limit, 5000)")
        applied["over_limit"] = is_over

    sql = "SELECT * FROM mandate_failures"
    if where:
        sql += " WHERE " + " AND ".join(where)

    conn = db.get_connection()
    try:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()

    # --- computed filters applied on the real rows --------------------------
    score_min = _to_number(spec.get("score_min"))
    score_max = _to_number(spec.get("score_max"))
    if score_min is not None or score_max is not None:
        lo = score_min if score_min is not None else 0
        hi = score_max if score_max is not None else 100
        rows = [r for r in rows if lo <= _row_score(r) <= hi]
        if score_min is not None:
            applied["score_min"] = lo
        if score_max is not None:
            applied["score_max"] = hi

    band = spec.get("health_band")
    if band in _VALID_VALUES["health_band"]:
        rows = [r for r in rows if _row_health_band(r) == band]
        applied["health_band"] = band

    # Enrich each row with the computed score + health band so the UI can show them.
    for r in rows:
        r["score"] = _row_score(r)
        r["health_band"] = _row_health_band(r)
        r["over_limit"] = float(r["amount"]) > float(r.get("mandate_limit") or 5000)

    # --- sort ---------------------------------------------------------------
    sort_dir = str(spec.get("sort_by_amount", "")).lower()
    if sort_dir == "asc":
        rows.sort(key=lambda r: float(r["amount"]))
        applied["sort_by_amount"] = "asc"
    else:
        # Default: highest amount first (most useful for an ops triage view).
        rows.sort(key=lambda r: float(r["amount"]), reverse=True)
        if sort_dir == "desc":
            applied["sort_by_amount"] = "desc"

    return rows[:limit], applied
