
"""Policy Center - Phase 7.
Per-merchant configurable recovery policies.
Extends existing adaptive_policy.py with merchant-facing UI-ready configuration.
"""
import json, logging, uuid
from datetime import datetime, timezone
log = logging.getLogger("mandate_rescue.policy_center")
_NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")

DEFAULTS = {
    "max_retries": 3,
    "retry_cooldown_hours": 24,
    "max_messages_per_week": 3,
    "preferred_channel": "email",
    "preferred_language": "en",
    "working_hours_start": 9,
    "working_hours_end": 20,
    "min_expected_value_rs": 0,
    "approval_threshold_rs": 10000,
    "escalation_overdue_days": 30,
    "escalation_amount_rs": 50000,
    "checkout_recovery_enabled": 1,
    "b2b_recovery_enabled": 1,
    "voice_recovery_enabled": 0,
}

ALLOWED_FIELDS = set(DEFAULTS.keys())


def get_merchant_policy(conn, merchant_id):
    """Return current policy for a merchant (or defaults)."""
    r = conn.execute(
        "SELECT * FROM merchant_recovery_policies WHERE merchant_id=?",
        (merchant_id,)).fetchone()
    if r:
        return dict(r)
    return dict(DEFAULTS, merchant_id=merchant_id)


def upsert_policy(conn, merchant_id, **updates):
    """Create or update merchant policy. Returns the active policy."""
    validated = {}
    errors = []
    for k, v in updates.items():
        if k not in ALLOWED_FIELDS:
            continue
        # Type coerce
        default_val = DEFAULTS.get(k)
        try:
            if isinstance(default_val, int):   v = int(v)
            elif isinstance(default_val, float): v = float(v)
            else:                               v = str(v)
        except (ValueError, TypeError):
            errors.append(f"Invalid value for {k}: {v}")
            continue
        # Bounds
        if k=="max_retries"           and not 1<=v<=10:  errors.append("max_retries 1-10"); continue
        if k=="retry_cooldown_hours"  and not 1<=v<=168: errors.append("cooldown 1-168h"); continue
        if k=="max_messages_per_week" and not 0<=v<=20:  errors.append("messages 0-20"); continue
        if k=="working_hours_start"   and not 0<=v<=23:  errors.append("start 0-23"); continue
        if k=="working_hours_end"     and not 0<=v<=23:  errors.append("end 0-23"); continue
        if k=="preferred_channel"     and v not in ("email","sms","whatsapp","in_app"):
            errors.append("channel: email|sms|whatsapp|in_app"); continue
        if k=="preferred_language"    and v not in ("en","hi","hinglish"):
            errors.append("language: en|hi|hinglish"); continue
        validated[k] = v
    if errors:
        return {"ok":False,"errors":errors}
    existing = conn.execute(
        "SELECT 1 FROM merchant_recovery_policies WHERE merchant_id=?",
        (merchant_id,)).fetchone()
    now = _NOW()
    if existing:
        if validated:
            validated["updated_at"] = now
            validated["version"] = conn.execute(
                "SELECT version FROM merchant_recovery_policies WHERE merchant_id=?",
                (merchant_id,)).fetchone()["version"] + 1
            sets = ", ".join(f"{k}=?" for k in validated)
            conn.execute(
                f"UPDATE merchant_recovery_policies SET {sets} WHERE merchant_id=?",
                list(validated.values())+[merchant_id])
    else:
        fields = dict(DEFAULTS)
        fields.update(validated)
        fields["merchant_id"] = merchant_id
        fields["created_at"]  = now
        fields["updated_at"]  = now
        cols = list(fields.keys())
        ph   = ", ".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO merchant_recovery_policies ({', '.join(cols)}) VALUES ({ph})",
            [fields[c] for c in cols])
    conn.commit()
    return {"ok":True,"policy":get_merchant_policy(conn,merchant_id)}


def reset_to_defaults(conn, merchant_id):
    """Reset merchant policy to system defaults."""
    conn.execute("DELETE FROM merchant_recovery_policies WHERE merchant_id=?", (merchant_id,))
    conn.commit()
    return upsert_policy(conn, merchant_id)
