"""Exportable summary report (design.md section 12, R12).

Builds a clean CSV containing the key dashboard metrics, the agent-vs-baseline
comparison, and the full exceptions list. All values come from real rows.
"""

import csv
import io

import db
import metrics
import baseline


def _pct(x):
    return f"{round(x * 100, 1)}%"


def build_summary_csv():
    """Return the summary report as a CSV string."""
    conn = db.get_connection()
    try:
        core = metrics.core_metrics(conn)
        base = baseline.run_baseline(conn)
        dumb = baseline.run_dumb_persistence_baseline(conn)
        exceptions = metrics.exceptions(conn)
        cohorts = metrics.cohorts(conn)
    finally:
        conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(["Mandate Rescue - Summary Report"])
    writer.writerow([])
    writer.writerow(["Key Metrics"])
    writer.writerow(["Metric", "Value"])
    writer.writerow(["Total cases", core["total_cases"]])
    writer.writerow(["Amount at risk (Rs)", core["amount_at_risk"]])
    writer.writerow(["Amount recovered (Rs)", core["amount_recovered"]])
    writer.writerow(["Recovery rate", _pct(core["recovery_rate"])])
    writer.writerow(["Escalation rate", _pct(core["escalation_rate"])])
    writer.writerow(["Amount recovery rate", _pct(core["amount_recovery_rate"])])
    writer.writerow([])
    writer.writerow(["Agent vs Baselines"])
    writer.writerow(["Approach", "Amount recovered (Rs)", "Recovery rate", "Retry attempts"])
    writer.writerow(["Naive baseline (1 attempt, no strategy)",
                     base["amount_recovered"], _pct(base["recovery_rate"]), "1 per case"])
    writer.writerow(["Dumb persistence (retry cap, no strategy)",
                     dumb["amount_recovered"], _pct(dumb["recovery_rate"]),
                     f"up to {dumb['retry_cap']} per case"])
    writer.writerow(["Mandate Rescue agent (retry cap + scoring + timing + dunning)",
                     core["amount_recovered"], _pct(core["recovery_rate"]),
                     f"up to {dumb['retry_cap']} per case"])
    writer.writerow([])
    writer.writerow(["Note: the 'dumb persistence' baseline isolates the value of "
                     "trying more times from the value of the agent's actual "
                     "scoring/strategy/timing/dunning intelligence. Agent minus dumb "
                     "persistence is the intelligence layer's real, defensible "
                     "contribution."])
    writer.writerow([])

    writer.writerow(["Recovery Rate by Tenure"])
    writer.writerow(["Segment", "Total", "Recovered", "Recovery rate"])
    for row in cohorts["by_tenure"]:
        writer.writerow([row["segment"], row["total"], row["recovered"], _pct(row["recovery_rate"])])
    writer.writerow([])
    writer.writerow(["Recovery Rate by Merchant Category"])
    writer.writerow(["Segment", "Total", "Recovered", "Recovery rate"])
    for row in cohorts["by_category"]:
        writer.writerow([row["segment"], row["total"], row["recovered"], _pct(row["recovery_rate"])])
    writer.writerow([])
    writer.writerow([f"Exceptions ({len(exceptions)}) - cases that ended unrecovered"])
    writer.writerow(["Customer", "Amount (Rs)", "Failure reason", "Category", "Status",
                     "Last action", "Why unrecovered"])
    for e in exceptions:
        writer.writerow([e["customer_id"], e["amount"], e["failure_reason"],
                         e["merchant_category"], e["case_status"],
                         e["last_action"], e["why_unrecovered"]])

    return buf.getvalue()
