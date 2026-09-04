
"""Payment Degradation Investigator + Revenue Investigator - Phase 7.
Strengthens existing anomaly detection with:
- Root cause analysis
- Revenue impact quantification
- Structured investigation questions
- Confident but honest causal language
"""
import json, logging
from datetime import datetime, timezone, timedelta
import db
import anomaly_detector as anom

log = logging.getLogger("mandate_rescue.investigator")


def investigate(conn, merchant_id, question=None, is_demo=0):
    """
    Structured revenue investigation.
    Answers questions like: Why did recovery rate drop? Which bank causes most losses?
    Returns: observation, evidence, likely_cause, revenue_impact, recommendation, confidence.
    Causal language is hedged: 'likely', 'associated with', 'strongest signal'.
    """
    cases = db.get_all_cases(conn)
    if is_demo:
        # For demo mode return demo-enriched data
        cases = [c for c in cases if c.get("is_demo") or True]  # demo includes all

    total = len(cases)
    if total == 0:
        return {"ok":False,"message":"No cases available for analysis."}

    # Run standard anomaly detection
    anomaly_report = anom.run_anomaly_detection(conn)

    # Revenue impact analysis
    active  = [c for c in cases if c["case_status"] not in ("recovered","rejected","invalid")]
    at_risk = sum(float(c["amount"]) for c in active)
    total_recovered  = sum(float(c["amount"]) for c in cases if c["case_status"]=="recovered")
    total_escalated  = sum(float(c["amount"]) for c in cases if c["case_status"] in ("escalated","broken_promise"))

    # Bank/method degradation signals
    bank_signals   = _bank_analysis(cases)
    method_signals = _method_analysis(cases)
    reason_signals = _reason_analysis(cases)

    # Build investigation result
    investigations = []

    # 1. Failure reason investigation
    for sig in reason_signals[:3]:
        investigations.append({
            "observation": f"{sig['reason']} failure rate: {sig['failure_rate']*100:.1f}%",
            "evidence": f"{sig['count']} cases, {sig['escalated']} escalated",
            "likely_cause": sig["likely_cause"],
            "revenue_impact_rs": round(sig["revenue_at_risk"], 2),
            "affected_cases": sig["count"],
            "recommendation": sig["recommendation"],
            "confidence": sig["confidence"],
            "data_type": "actual",
            "causal_note": "Association detected, not proven causation.",
        })

    # 2. Bank degradation signals
    for sig in bank_signals[:2]:
        investigations.append({
            "observation": f"Bank '{sig['bank']}' associated with elevated failures",
            "evidence": f"{sig['count']} cases with bank={sig['bank']}, {sig['failure_rate']*100:.1f}% failure rate",
            "likely_cause": "Possible bank-side issue or UPI rail degradation for this bank",
            "revenue_impact_rs": round(sig["revenue_at_risk"],2),
            "affected_cases": sig["count"],
            "recommendation": "Consider routing to alternate payment method for affected customers",
            "confidence": "moderate" if sig["count"]>10 else "low",
            "data_type": "actual",
            "causal_note": "Strongest observed signal. Causation not confirmed — bank may have an outage or rate limit.",
        })

    # 3. Anomaly alerts
    for alert in anomaly_report.get("alerts",[])[:3]:
        investigations.append({
            "observation": alert["title"],
            "evidence": alert["description"],
            "likely_cause": alert.get("recommended_action","Review required"),
            "revenue_impact_rs": 0,
            "affected_cases": alert.get("evidence",{}).get("n",0),
            "recommendation": alert.get("recommended_action",""),
            "confidence": "moderate" if alert["severity"]=="critical" else "low",
            "data_type": "actual",
            "causal_note": "Statistical anomaly. Manual review recommended.",
        })

    return {
        "ok": True,
        "question": question,
        "total_cases": total,
        "revenue_at_risk_rs": round(at_risk,2),
        "recovered_revenue_rs": round(total_recovered,2),
        "escalated_revenue_rs": round(total_escalated,2),
        "investigations": investigations,
        "anomaly_summary": {
            "total": anomaly_report["total"],
            "has_critical": anomaly_report["has_critical"],
        },
        "data_type": "actual",
        "causal_language_note": (
            "All causal claims use hedged language (likely, associated with, "
            "strongest signal). Causation is not claimed without experimental evidence."
        ),
    }


def _reason_analysis(cases):
    buckets = {}
    for c in cases:
        r = c.get("failure_reason","unknown")
        b = buckets.setdefault(r, {"count":0,"recovered":0,"escalated":0,"amount":0.0})
        b["count"]+=1
        if c["case_status"]=="recovered": b["recovered"]+=1
        elif c["case_status"] in ("escalated","broken_promise"): b["escalated"]+=1
        b["amount"]+=float(c.get("amount",0))
    results = []
    for reason,b in buckets.items():
        fr = 1.0-b["recovered"]/b["count"] if b["count"]>0 else 0
        likely = {
            "insufficient_funds":"Likely timing issue — payment attempted before salary credit",
            "mandate_expired":"Mandate TTL issue — customer needs to re-authorize",
            "bank_technical_error":"Likely transient bank/UPI rail issue",
            "mandate_revoked":"Customer explicitly cancelled mandate",
        }.get(reason,"Insufficient data for likely cause determination")
        rec = {
            "insufficient_funds":"Schedule retries at salary-credit windows",
            "mandate_expired":"Send re-authorization link immediately",
            "bank_technical_error":"Auto-retry after 2h; no customer contact needed",
            "mandate_revoked":"Escalate to manual outreach",
        }.get(reason,"Investigate case-by-case")
        results.append({"reason":reason,"count":b["count"],"failure_rate":fr,
                         "recovered":b["recovered"],"escalated":b["escalated"],
                         "revenue_at_risk":b["amount"]*(1-b["recovered"]/b["count"] if b["count"] else 1),
                         "likely_cause":likely,"recommendation":rec,
                         "confidence":"moderate" if b["count"]>10 else "low"})
    return sorted(results, key=lambda x:-x["revenue_at_risk"])


def _bank_analysis(cases):
    buckets = {}
    for c in cases:
        bank = c.get("bank_name") or c.get("payment_method") or "unknown"
        b = buckets.setdefault(bank,{"count":0,"recovered":0,"amount":0.0})
        b["count"]+=1
        if c["case_status"]=="recovered": b["recovered"]+=1
        b["amount"]+=float(c.get("amount",0))
    results = []
    for bank,b in buckets.items():
        if bank=="unknown" or b["count"]<3: continue
        fr = 1-b["recovered"]/b["count"]
        results.append({"bank":bank,"count":b["count"],"failure_rate":fr,
                         "revenue_at_risk":b["amount"]*fr})
    return sorted(results, key=lambda x:-x["revenue_at_risk"])


def _method_analysis(cases):
    buckets = {}
    for c in cases:
        m = c.get("payment_method") or "unknown"
        b = buckets.setdefault(m,{"count":0,"recovered":0,"amount":0.0})
        b["count"]+=1
        if c["case_status"]=="recovered": b["recovered"]+=1
        b["amount"]+=float(c.get("amount",0))
    results = []
    for method,b in buckets.items():
        if b["count"]<3: continue
        fr = 1-b["recovered"]/b["count"]
        results.append({"method":method,"count":b["count"],"failure_rate":fr,
                         "revenue_at_risk":b["amount"]*fr})
    return sorted(results, key=lambda x:-x["revenue_at_risk"])


def record_degradation_event(conn, merchant_id, degradation_type, affected_segment,
                              what_changed, severity="warning", revenue_at_risk=0,
                              affected_cases=0, recommended_action=None,
                              confidence=0.5, evidence=None, is_demo=0):
    """Record a payment degradation event for tracking and notification."""
    import uuid
    event_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO payment_degradation_events
           (event_id,merchant_id,degradation_type,affected_segment,severity,
            what_changed,revenue_at_risk,affected_cases,recommended_action,
            confidence,evidence,status,data_type,is_demo)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,'active','actual',?)""",
        (event_id,merchant_id,degradation_type,affected_segment,severity,
         what_changed,revenue_at_risk,affected_cases,recommended_action,
         confidence,json.dumps(evidence) if evidence else None,is_demo))
    return event_id


def get_degradation_events(conn, merchant_id, status="active", is_demo=None):
    cl,pa = ["merchant_id=?"],[merchant_id]
    if status: cl.append("status=?"); pa.append(status)
    if is_demo is not None: cl.append("is_demo=?"); pa.append(is_demo)
    rows = conn.execute(
        f"SELECT * FROM payment_degradation_events WHERE {' AND '.join(cl)} "
        "ORDER BY detected_at DESC LIMIT 50", pa).fetchall()
    return [dict(r) for r in rows]
