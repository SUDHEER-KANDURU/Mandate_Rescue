
"""Channel Decisioning Engine + Voice-Ready Abstraction - Phase 7.
Selects the optimal recovery channel based on case context, merchant policy,
economic value, and customer engagement history.
Voice provider interface: READY_FOR_PROVIDER but no real calls made unless
a provider adapter is plugged in.
"""
import json, logging, uuid
from datetime import datetime, timezone
log = logging.getLogger("mandate_rescue.channel")
_NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")

CHANNEL_EMAIL    = "email"
CHANNEL_SMS      = "sms"
CHANNEL_WHATSAPP = "whatsapp"
CHANNEL_IN_APP   = "in_app"
CHANNEL_VOICE    = "voice"
ALL_CHANNELS     = [CHANNEL_EMAIL,CHANNEL_SMS,CHANNEL_WHATSAPP,CHANNEL_IN_APP,CHANNEL_VOICE]

# Configurable channel costs (Rs)
CHANNEL_COSTS = {
    CHANNEL_EMAIL:    0.10,
    CHANNEL_SMS:      0.50,
    CHANNEL_WHATSAPP: 0.75,
    CHANNEL_IN_APP:   0.05,
    CHANNEL_VOICE:    5.00,
}
# Baseline engagement rates (ESTIMATED from industry benchmarks)
CHANNEL_ENGAGE = {
    CHANNEL_EMAIL:    0.25,
    CHANNEL_SMS:      0.40,
    CHANNEL_WHATSAPP: 0.55,
    CHANNEL_IN_APP:   0.35,
    CHANNEL_VOICE:    0.60,
}


def select_channel(case, policy, recovery_prob=None):
    """Select the best channel for a recovery case.
    Returns selection dict with rationale and expected net value per channel.
    data_type=ESTIMATED (all EVs are model-based).
    """
    amount    = float(case.get("amount") or 0)
    scenario  = case.get("scenario_type","")
    priority  = case.get("priority","medium")
    pref      = policy.get("preferred_channel", CHANNEL_EMAIL)
    voice_ok  = bool(policy.get("voice_recovery_enabled",0))
    prob      = recovery_prob or float(case.get("recovery_probability") or 0.40)
    candidates = _candidate_channels(scenario, priority, voice_ok, policy)
    scored = []
    for ch in candidates:
        ev = _channel_ev(ch, prob, amount)
        scored.append({
            "channel": ch,
            "expected_net_value_rs": ev,
            "cost_rs": CHANNEL_COSTS.get(ch,0.5),
            "engagement_rate": CHANNEL_ENGAGE.get(ch,0.3),
            "available": True,
        })
    scored.sort(key=lambda x: x["expected_net_value_rs"], reverse=True)
    best = scored[0] if scored else {"channel": pref}
    rationale = _rationale(best["channel"], scenario, priority, amount)
    return {
        "selected_channel": best["channel"],
        "rationale": rationale,
        "expected_net_value_rs": best.get("expected_net_value_rs",0),
        "channels_considered": scored,
        "data_type": "ESTIMATED",
        "note": "Channel selection and EV are ESTIMATED from engagement rate baselines.",
    }


def _channel_ev(channel, prob, amount):
    """Expected Net Value = P(recovery|channel) * amount - channel_cost. ESTIMATED."""
    engage = CHANNEL_ENGAGE.get(channel, 0.30)
    p_with_channel = min(prob * (1 + engage * 0.5), 0.99)
    gross = p_with_channel * amount
    cost  = CHANNEL_COSTS.get(channel, 0.50)
    return round(gross - cost, 2)


def _candidate_channels(scenario, priority, voice_ok, policy):
    channels = [CHANNEL_EMAIL, CHANNEL_SMS, CHANNEL_IN_APP]
    if policy.get("preferred_channel") == CHANNEL_WHATSAPP:
        channels.append(CHANNEL_WHATSAPP)
    if voice_ok and priority in ("critical","high"):
        channels.append(CHANNEL_VOICE)
    return channels


def _rationale(channel, scenario, priority, amount):
    lines = {
        CHANNEL_EMAIL:    f"Email selected: standard channel, low cost, suitable for {scenario}.",
        CHANNEL_SMS:      f"SMS selected: high open rate, appropriate for {priority} priority.",
        CHANNEL_WHATSAPP: f"WhatsApp selected: highest engagement rate, merchant preference.",
        CHANNEL_IN_APP:   f"In-app selected: zero delivery cost, immediate visibility.",
        CHANNEL_VOICE:    f"Voice selected: highest P(engagement) for critical Rs{amount:,.0f} case.",
    }
    return lines.get(channel, f"{channel} selected based on merchant policy.")


def record_channel_decision(conn, case_id, merchant_id, selection):
    """Persist channel decision to audit trail."""
    dec_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO channel_decisions
           (decision_id,case_id,merchant_id,selected_channel,rationale,
            expected_recovery_prob,expected_net_value,channels_considered)
           VALUES (?,?,?,?,?,?,?,?)""",
        (dec_id, case_id, merchant_id,
         selection.get("selected_channel"),
         selection.get("rationale"),
         selection.get("expected_recovery_prob"),
         selection.get("expected_net_value_rs"),
         json.dumps(selection.get("channels_considered",[]))))
    return dec_id


# ---- Voice-Ready Abstraction ----

def create_voice_script(conn, case_id, merchant_id, language="en",
                         call_intent="recovery_reminder"):
    """Generate a voice script and record it. No actual call made.
    Status: SCRIPT_GENERATED -> READY_FOR_PROVIDER -> SIMULATED/PROVIDER_EXECUTION
    """
    import recovery_orchestrator as orch
    from multilingual import generate_voice_script
    case = orch.get_case(conn, case_id, merchant_id)
    if not case: return {"ok":False,"error":"case_not_found"}
    result = generate_voice_script(case, language=language, call_intent=call_intent)
    script_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO voice_scripts
           (script_id,case_id,merchant_id,language,script_text,call_intent,status)
           VALUES (?,?,?,?,?,?,'READY_FOR_PROVIDER')""",
        (script_id, case_id, merchant_id, language,
         result["script_text"], call_intent))
    orch._event(conn, case_id, merchant_id, "action_scheduled",
                f"Voice script generated [{language}] intent={call_intent}",
                metadata={"script_id":script_id,"language":language},
                data_type="SIMULATED")
    return {"ok":True,"script_id":script_id,**result}


def simulate_voice_outcome(conn, script_id, outcome="no_response"):
    """Simulate a voice call outcome for demo/testing. Clearly SIMULATED.
    outcome: answered_paid | answered_promised | no_answer | no_response
    """
    conn.execute(
        "UPDATE voice_scripts SET status='SIMULATED',simulated_outcome=? WHERE script_id=?",
        (outcome, script_id))
    return {"ok":True,"script_id":script_id,"simulated_outcome":outcome,
            "data_type":"SIMULATED",
            "note":"This outcome is SIMULATED. No real call was made."}
