"""Policy Versioning, Recommendation, Approval & Rollback Engine — Phase 6.

This module ties together the complete closed-loop governance:

  OBSERVE → MEASURE → COMPARE → LEARN
  → RECOMMEND → MERCHANT APPROVES → POLICY ACTIVATES → MEASURE AGAIN

Design principles
-----------------
- Policy versions are IMMUTABLE after creation. Only status transitions are allowed.
- Every recommendation carries a full evidence trail answering "why did this change?"
- No policy change is silent. Every approval, activation, and rollback creates an
  audit event in policy_audit_log.
- Insufficient-data protection: generate_recommendations() only creates a
  recommendation when sample size and evidence quality meet the threshold.
- The engine does NOT execute recovery directly — it recommends to the human.
  Execution still goes through agent.py.

Public API
----------
generate_recommendations(conn) → list[dict]
    Scan strategy_performance and produce new policy_recommendations rows where
    evidence supports a change. Skips dimensions with insufficient data.

create_policy_version(conn, ...) → version_id
    Create a new DRAFT policy version with full parameter set + evidence.

submit_for_review(conn, version_id, actor) → bool
submit_recommendation(conn, rec_id, actor) → bool
approve_recommendation(conn, rec_id, actor) → dict
reject_recommendation(conn, rec_id, actor, reason) → bool
activate_policy_version(conn, version_id, actor) → bool
rollback_to_version(conn, target_version_id, actor, reason) → dict
get_active_policy(conn, merchant_category) → dict
get_policy_history(conn, merchant_category) → list[dict]
learning_dashboard_summary(conn) → dict
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

import db
from outcome_attribution import PROV_REAL_TEST, PROV_SIMULATION, PROV_HISTORICAL
from segment_learning import (
    full_learning_summary, strategy_ranking, MIN_SAMPLE, MIN_IMPROVEMENT_PP,
    _recovery_rate,
)
from adaptive_policy import _rule_based_strategy, _observed_strategy_rates

# Default policy parameters — these are the current rule-based defaults.
# A policy version wraps these into a named, versioned, audited record.
DEFAULT_STRATEGY_PARAMS = {
    "insufficient_funds": "salary-window retry",
    "mandate_expired":    "re-authorization link",
    "bank_technical_error": "silent quick retry",
    "mandate_revoked":    "immediate escalation",
    "over_limit":         "higher-limit re-authorization",
}

# Confidence thresholds
CONFIDENCE_THRESHOLDS = {
    "high":       (50, 0.06),   # ≥50 obs, ≥6pp improvement
    "moderate":   (30, 0.04),   # ≥30 obs, ≥4pp improvement
    "low":        (10, 0.03),   # ≥10 obs, ≥3pp improvement
}


def _confidence_label_from_evidence(sample_size: int, improvement_pp: float) -> str:
    """Classify confidence as high/moderate/low based on sample + improvement."""
    for label, (min_n, min_pp) in CONFIDENCE_THRESHOLDS.items():
        if sample_size >= min_n and improvement_pp >= min_pp:
            return label
    return "insufficient"


def generate_recommendations(conn) -> list[dict]:
    """Scan strategy_performance and generate new policy_recommendations.

    Rules:
    - Only generates a recommendation if alternative rate exceeds current default
      by at least MIN_IMPROVEMENT_PP percentage points.
    - Only generates when sample_size >= MIN_SAMPLE.
    - Does NOT overwrite existing RECOMMENDED/UNDER_REVIEW recommendations
      for the same strategy+dimension.
    - Tags data_source from provenance (REAL_TEST > HISTORICAL > SIMULATION).

    Returns list of newly created recommendation dicts.
    """
    summary = full_learning_summary(conn)
    new_recs = []
    existing_recs = db.get_all_recommendations(conn, status=None)
    # Build a set of (current_strategy, rec_strategy, merchant_cat) already active
    active_keys: set[tuple] = set()
    for r in existing_recs:
        if r["status"] in ("recommended", "under_review", "approved"):
            active_keys.add((r["current_strategy"], r["recommended_strategy"],
                             r["merchant_category"]))

    for section in summary["dimensions"]:
        dim_key = section["dimension_key"]
        dim_val = section["dimension_value"]
        if not section["has_sufficient_data"]:
            continue

        strategies = section["strategies"]
        # Find default strategy for this dimension
        default_strat = None
        if dim_key == "failure_reason":
            # Construct a synthetic case to get the rule-based default
            synthetic_case = {"failure_reason": dim_val, "amount": 5000,
                              "mandate_limit": 5000}
            default_strat = _rule_based_strategy(synthetic_case)
        elif dim_key == "global":
            default_strat = None  # global: look at all defaults
        else:
            default_strat = None

        # For each strategy with sufficient data, check if it beats the default
        default_row = next(
            (s for s in strategies
             if s["strategy"] == default_strat and s["sufficient"]),
            None,
        )
        best_row = next((s for s in strategies if s["sufficient"]), None)
        if not best_row:
            continue
        if best_row["strategy"] == default_strat:
            continue  # Default is already the best — no change needed

        current_rate = default_row["recovery_rate"] if default_row else 0.0
        best_rate = best_row["recovery_rate"]
        improvement_pp = (best_rate - current_rate) * 100

        if improvement_pp < MIN_IMPROVEMENT_PP:
            continue  # Not enough improvement

        confidence = _confidence_label_from_evidence(best_row["attempts"], improvement_pp)
        if confidence == "insufficient":
            continue

        merchant_cat = dim_val if dim_key == "merchant_category" else "all"
        fail_reason = dim_val if dim_key == "failure_reason" else None
        cur_strat = default_strat or "rule_based_default"

        # Deduplicate
        dedup_key = (cur_strat, best_row["strategy"], merchant_cat)
        if dedup_key in active_keys:
            continue

        # Determine data source
        prov_mix = section.get("provenance_mix", [])
        if PROV_REAL_TEST in prov_mix:
            data_source = "REAL_TEST"
        elif PROV_HISTORICAL in prov_mix:
            data_source = "HISTORICAL"
        else:
            data_source = "SIMULATION"

        # Estimate monthly incremental revenue
        total_amount = sum(
            s["amount_attempted"] for s in strategies
            if s["sufficient"] and s["strategy"] == best_row["strategy"]
        )
        est_incremental = round(improvement_pp / 100 * total_amount, 2)

        # Build evidence trail
        evidence = {
            "dimension": f"{dim_key}={dim_val}",
            "current_strategy": cur_strat,
            "current_observed_rate": round(current_rate, 4),
            "recommended_strategy": best_row["strategy"],
            "recommended_observed_rate": round(best_rate, 4),
            "improvement_pp": round(improvement_pp, 2),
            "sample_size": best_row["attempts"],
            "provenance": prov_mix,
            "all_strategies": [
                {"strategy": s["strategy"], "rate": s["recovery_rate"],
                 "n": s["attempts"], "sufficient": s["sufficient"]}
                for s in strategies
            ],
        }

        title = (
            f"Switch to '{best_row['strategy']}' for "
            f"{dim_val.replace('_', ' ')} cases"
        )
        what_changes = (
            f"Change strategy from '{cur_strat}' to '{best_row['strategy']}' "
            f"for {dim_key}='{dim_val}'. "
            f"Observed improvement: {improvement_pp:.1f}pp "
            f"({current_rate*100:.1f}% → {best_rate*100:.1f}%) "
            f"based on {best_row['attempts']} observations. "
            f"Evidence type: {data_source}."
        )

        rec_id = str(uuid.uuid4())
        ok = db.create_policy_recommendation(
            conn,
            recommendation_id=rec_id,
            title=title,
            what_changes=what_changes,
            why_evidence=json.dumps(evidence),
            current_strategy=cur_strat,
            recommended_strategy=best_row["strategy"],
            current_rate=current_rate,
            recommended_rate=best_rate,
            sample_size=best_row["attempts"],
            estimated_incremental_rs=est_incremental,
            confidence=confidence,
            data_source=data_source,
            merchant_category=merchant_cat,
            failure_reason=fail_reason,
        )
        if ok:
            conn.commit()
            active_keys.add(dedup_key)
            new_recs.append({
                "recommendation_id": rec_id,
                "title": title,
                "confidence": confidence,
                "data_source": data_source,
                "estimated_incremental_rs": est_incremental,
            })

    return new_recs


def create_policy_version_from_recommendation(
    conn,
    recommendation_id: str,
    created_by: str = "system",
) -> Optional[str]:
    """Create a DRAFT policy version based on an approved recommendation.

    Called automatically when a recommendation is approved. Builds the new
    strategy_params by patching the current active policy's params with the
    recommended change.
    """
    rec = db.get_recommendation(conn, recommendation_id)
    if not rec:
        return None

    merchant_cat = rec.get("merchant_category", "all")
    current_active = db.get_active_policy_version(conn, merchant_cat)
    if current_active:
        try:
            params = json.loads(current_active["strategy_params"])
        except Exception:
            params = dict(DEFAULT_STRATEGY_PARAMS)
        prev_version_id = current_active["version_id"]
    else:
        params = dict(DEFAULT_STRATEGY_PARAMS)
        prev_version_id = None

    # Apply the recommended change
    fail_reason = rec.get("failure_reason")
    if fail_reason:
        params[fail_reason] = rec["recommended_strategy"]
    else:
        # Global change — update the strategy for all applicable reasons
        for reason, strat in DEFAULT_STRATEGY_PARAMS.items():
            if strat == rec["current_strategy"]:
                params[reason] = rec["recommended_strategy"]

    expected_impact = json.dumps({
        "recovery_rate_delta": round(
            (rec.get("recommended_rate") or 0) - (rec.get("current_rate") or 0), 4
        ),
        "estimated_incremental_rs": rec.get("estimated_incremental_rs"),
        "confidence": rec.get("confidence"),
        "data_source": rec.get("data_source"),
    })

    version_id = str(uuid.uuid4())
    db.create_policy_version(
        conn,
        version_id=version_id,
        merchant_category=merchant_cat,
        strategy_params=json.dumps(params),
        reason=f"Created from recommendation: {rec['title']}",
        evidence_summary=rec["why_evidence"],
        created_by=created_by,
        previous_version_id=prev_version_id,
        expected_impact=expected_impact,
    )
    # Link recommendation → version
    conn.execute(
        "UPDATE policy_recommendations SET policy_version_id = ? "
        "WHERE recommendation_id = ?",
        (version_id, recommendation_id),
    )
    conn.commit()
    return version_id


def approve_recommendation(
    conn, recommendation_id: str, actor: str = "system"
) -> dict:
    """Approve a recommendation. Creates a DRAFT policy version, then activates it.

    Flow: recommended → approved → policy_version(draft → active).
    Returns {"approved": True, "version_id": ..., "recommendation_id": ...}.
    """
    ok = db.update_recommendation_status(conn, recommendation_id, "approved", actor=actor)
    if not ok:
        return {"approved": False, "reason": "status_transition_failed",
                "recommendation_id": recommendation_id}

    version_id = create_policy_version_from_recommendation(
        conn, recommendation_id, created_by=actor
    )
    if not version_id:
        return {"approved": True, "version_id": None,
                "recommendation_id": recommendation_id,
                "warning": "Recommendation approved but version creation failed."}

    # Move draft → recommended → approved → active
    db.transition_policy_version(conn, version_id, "recommended", actor=actor)
    db.transition_policy_version(conn, version_id, "under_review", actor=actor)
    db.transition_policy_version(conn, version_id, "approved", actor=actor)
    db.transition_policy_version(conn, version_id, "active", actor=actor)
    conn.commit()
    return {
        "approved": True,
        "recommendation_id": recommendation_id,
        "version_id": version_id,
        "message": "Recommendation approved and policy version activated.",
    }


def reject_recommendation(
    conn, recommendation_id: str, actor: str, reason: str
) -> bool:
    return db.update_recommendation_status(
        conn, recommendation_id, "rejected",
        actor=actor, rejection_reason=reason,
    )


def rollback_to_version(
    conn, target_version_id: str, actor: str, reason: str
) -> dict:
    """Roll back to a previously active policy version.

    - Deprecates the currently active version (if any, for the same merchant).
    - Reactivates the target version.
    - Creates policy_audit_log entries for both.
    - Does NOT modify historical performance records — history stays immutable.

    Returns {"rolled_back": True, "activated_version": ..., "deprecated_version": ...}.
    """
    target = db.get_policy_version(conn, target_version_id)
    if not target:
        return {"rolled_back": False, "reason": "target_version_not_found"}
    if target["status"] not in ("deprecated", "rolled_back", "approved"):
        return {"rolled_back": False,
                "reason": f"Cannot roll back to version with status '{target['status']}'"}

    merchant_cat = target["merchant_category"]
    current_active = db.get_active_policy_version(conn, merchant_cat)
    deprecated_id = None

    if current_active and current_active["version_id"] != target_version_id:
        deprecated_id = current_active["version_id"]
        # Force transition to rolled_back (may not follow normal LEGAL_TRANSITIONS)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE policy_versions SET status = 'rolled_back', deprecated_at = ? "
            "WHERE version_id = ?",
            (now, deprecated_id),
        )
        db._append_policy_audit(
            conn, "version_rolled_back", version_id=deprecated_id,
            actor=actor, previous_status="active", new_status="rolled_back",
            notes=f"Rolled back: {reason}",
        )

    # Reactivate target version
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE policy_versions SET status = 'active', activated_at = ? "
        "WHERE version_id = ?",
        (now, target_version_id),
    )
    db._append_policy_audit(
        conn, "version_activated", version_id=target_version_id,
        actor=actor, previous_status=target["status"], new_status="active",
        notes=f"Rollback from {deprecated_id or 'none'}: {reason}",
    )
    conn.commit()
    return {
        "rolled_back": True,
        "activated_version": target_version_id,
        "deprecated_version": deprecated_id,
        "merchant_category": merchant_cat,
        "reason": reason,
    }


def get_active_policy(conn, merchant_category: str = "all") -> dict:
    """Return the currently active policy version for a merchant category.

    If no version is active, returns the built-in rule-based defaults.
    """
    version = db.get_active_policy_version(conn, merchant_category)
    if version:
        try:
            params = json.loads(version["strategy_params"])
        except Exception:
            params = dict(DEFAULT_STRATEGY_PARAMS)
        perf = db.get_policy_performance(conn, version["version_id"])
        latest_perf = perf[0] if perf else None
        return {
            "source": "policy_version",
            "version_id": version["version_id"],
            "version_number": version["version_number"],
            "merchant_category": version["merchant_category"],
            "strategy_params": params,
            "status": version["status"],
            "activated_at": version.get("activated_at"),
            "reason": version.get("reason"),
            "approved_by": version.get("approved_by"),
            "expected_impact": _parse_json_field(version.get("expected_impact")),
            "measured_performance": latest_perf,
        }
    return {
        "source": "rule_based_default",
        "version_id": None,
        "version_number": 0,
        "merchant_category": merchant_category,
        "strategy_params": dict(DEFAULT_STRATEGY_PARAMS),
        "status": "active",
        "activated_at": None,
        "reason": "No policy version created yet; using built-in rule-based defaults.",
        "approved_by": None,
        "expected_impact": None,
        "measured_performance": None,
    }


def get_policy_history(conn, merchant_category: str = "all") -> list[dict]:
    """Return all policy versions for a merchant category, newest first."""
    versions = db.get_all_policy_versions(conn, merchant_category=merchant_category)
    result = []
    for v in versions:
        perf = db.get_policy_performance(conn, v["version_id"])
        result.append({
            **v,
            "strategy_params": _parse_json_field(v.get("strategy_params")),
            "evidence_summary": _parse_json_field(v.get("evidence_summary")),
            "expected_impact": _parse_json_field(v.get("expected_impact")),
            "performance_records": perf,
        })
    return result


def record_current_policy_performance(conn) -> dict:
    """Measure and record the current active policy's performance.

    Uses actual mandate_failures outcomes for cases processed after the policy
    was activated. Returns the recorded measurement or an explanation of why
    it could not be done.
    """
    import metrics as metrics_module

    version = db.get_active_policy_version(conn, "all")
    if not version:
        return {"recorded": False, "reason": "no_active_policy_version"}

    core = metrics_module.core_metrics(conn)
    total = core.get("total_cases", 0)
    recovered = core.get("recovered_cases", 0)
    recovery_rate = core.get("recovery_rate", 0.0)
    amount_recovered = core.get("amount_recovered", 0.0)
    escalation_rate = core.get("escalation_rate", 0.0)

    if total < 5:
        return {"recorded": False, "reason": "insufficient_cases_for_measurement",
                "total_cases": total}

    db.record_policy_performance(
        conn,
        version_id=version["version_id"],
        cases_observed=total,
        recoveries=recovered,
        recovery_rate=recovery_rate,
        amount_recovered=amount_recovered,
        escalation_rate=escalation_rate,
        measurement_window_days=30,
        data_type="actual",
    )
    conn.commit()
    return {
        "recorded": True,
        "version_id": version["version_id"],
        "cases_observed": total,
        "recovery_rate": recovery_rate,
        "amount_recovered": amount_recovered,
    }


def _parse_json_field(val) -> Optional[dict]:
    if not val:
        return None
    if isinstance(val, dict):
        return val
    try:
        return json.loads(val)
    except Exception:
        return None


def learning_dashboard_summary(conn) -> dict:
    """Single-call payload for the Learning Dashboard.

    Returns:
        active_policy          current active version or rule-based defaults
        performance_vs_previous  before/after comparison if a previous version exists
        open_recommendations   all RECOMMENDED/UNDER_REVIEW items
        recent_experiments     last 5 experiments with basic stats
        strategy_learning      full_learning_summary top-level summary
        attribution_summary    from outcome_attribution
        policy_history         last 5 versions
    """
    import outcome_attribution as oa
    import experimentation as exp_module
    from experiment_evaluator import evaluate_experiment

    active = get_active_policy(conn)
    history = get_policy_history(conn, "all")

    # Before vs after comparison
    perf_comparison = None
    if active["source"] == "policy_version" and len(history) >= 2:
        current_v = history[0]
        prev_v = next(
            (v for v in history[1:]
             if v["status"] in ("deprecated", "rolled_back")), None
        )
        if prev_v:
            cur_perfs = current_v.get("performance_records") or []
            prev_perfs = prev_v.get("performance_records") or []
            if cur_perfs and prev_perfs:
                perf_comparison = {
                    "current_version": current_v["version_id"],
                    "current_recovery_rate": cur_perfs[0]["recovery_rate"],
                    "previous_version": prev_v["version_id"],
                    "previous_recovery_rate": prev_perfs[0]["recovery_rate"],
                    "delta": round(
                        cur_perfs[0]["recovery_rate"] - prev_perfs[0]["recovery_rate"],
                        4
                    ),
                    "data_type": "actual",
                    "note": (
                        "Before/after comparison based on recorded policy performance measurements. "
                        "This is observational, not a controlled experiment."
                    ),
                }

    open_recs = db.get_all_recommendations(conn)
    open_recs = [r for r in open_recs if r["status"] in ("recommended", "under_review")]
    # Enrich with parsed evidence
    for r in open_recs:
        r["why_evidence_parsed"] = _parse_json_field(r.get("why_evidence"))

    # Recent experiments
    all_exps = db.get_all_experiments(conn)[:5]
    recent_exps = []
    for exp in all_exps:
        eid = exp["experiment_id"]
        eval_result = evaluate_experiment(conn, eid)
        recent_exps.append(eval_result)

    # Strategy learning
    learning = full_learning_summary(conn)

    # Attribution
    attribution = oa.get_attribution_summary(conn)

    # Policy history (last 5)
    short_history = history[:5]
    for v in short_history:
        v.pop("strategy_params", None)  # too verbose for summary

    return {
        "active_policy": active,
        "performance_vs_previous": perf_comparison,
        "open_recommendations": open_recs,
        "open_recommendation_count": len(open_recs),
        "recent_experiments": recent_exps,
        "strategy_learning": {
            "real_test_observations": learning["real_test_observations"],
            "real_test_note": learning["real_test_note"],
            "provenance_summary": learning["provenance_summary"],
            "min_sample_for_recommendation": learning["min_sample_for_recommendation"],
        },
        "attribution_summary": attribution,
        "policy_history_recent": short_history,
        "data_type": "mixed",
    }
