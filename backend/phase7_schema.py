"""Phase 7 database schema extensions.

New tables for the unified Revenue Recovery OS:
  - recovery_cases     Unified recovery case for all scenario types
  - checkout_sessions  Checkout abandonment tracking
  - b2b_invoices       B2B receivables
  - promises           Promise-to-pay tracker
  - recovery_actions   All recovery actions taken on cases
  - channel_decisions  Channel decisioning audit trail
  - voice_scripts      Voice-ready script generation
  - demo_sessions      Demo mode isolation

All merchant-facing tables include merchant_id for data isolation.
"""

PHASE7_SCHEMA = """
-- ---------------------------------------------------------------------------
-- recovery_cases: unified case for all 7 scenario types
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recovery_cases (
    case_id               TEXT    PRIMARY KEY,
    merchant_id           TEXT    NOT NULL,
    scenario_type         TEXT    NOT NULL,
    customer_ref          TEXT,
    customer_name         TEXT,
    customer_email        TEXT,
    customer_phone        TEXT,
    amount                REAL    NOT NULL DEFAULT 0,
    amount_at_risk        REAL    NOT NULL DEFAULT 0,
    currency              TEXT    NOT NULL DEFAULT 'INR',
    status                TEXT    NOT NULL DEFAULT 'open',
    priority              TEXT    NOT NULL DEFAULT 'medium',
    risk_score            REAL,
    root_cause            TEXT,
    root_cause_confidence REAL,
    recommended_action    TEXT,
    selected_strategy     TEXT,
    policy_version_id     TEXT,
    experiment_id         TEXT,
    experiment_arm        TEXT,
    expected_recovery_value REAL,
    realized_value        REAL    DEFAULT 0,
    recovery_probability  REAL,
    preferred_channel     TEXT,
    last_channel_used     TEXT,
    communication_count   INTEGER NOT NULL DEFAULT 0,
    last_contacted_at     TEXT,
    source                TEXT    NOT NULL DEFAULT 'SIMULATED',
    failure_reason        TEXT,
    payment_method        TEXT,
    bank_name             TEXT,
    geography             TEXT,
    merchant_category     TEXT,
    mandate_customer_id   TEXT,
    razorpay_payment_id   TEXT,
    razorpay_subscription_id TEXT,
    approval_required     INTEGER NOT NULL DEFAULT 0,
    approval_status       TEXT    DEFAULT 'not_required',
    approved_by           TEXT,
    approved_at           TEXT,
    approval_notes        TEXT,
    ai_explanation        TEXT,
    ai_explanation_at     TEXT,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    updated_at            TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    resolved_at           TEXT,
    due_at                TEXT,
    is_demo               INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rc_merchant    ON recovery_cases(merchant_id);
CREATE INDEX IF NOT EXISTS idx_rc_status      ON recovery_cases(status);
CREATE INDEX IF NOT EXISTS idx_rc_scenario    ON recovery_cases(scenario_type);
CREATE INDEX IF NOT EXISTS idx_rc_priority    ON recovery_cases(priority);
CREATE INDEX IF NOT EXISTS idx_rc_created     ON recovery_cases(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rc_mandate_cid ON recovery_cases(mandate_customer_id);
CREATE INDEX IF NOT EXISTS idx_rc_demo        ON recovery_cases(is_demo);

-- ---------------------------------------------------------------------------
-- recovery_case_events: append-only timeline for a recovery case
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recovery_case_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT    NOT NULL,
    merchant_id TEXT    NOT NULL,
    event_type  TEXT    NOT NULL,
    description TEXT    NOT NULL,
    actor       TEXT    NOT NULL DEFAULT 'system',
    metadata    TEXT,
    data_type   TEXT    NOT NULL DEFAULT 'SIMULATED',
    occurred_at TEXT    NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_rce_case       ON recovery_case_events(case_id);
CREATE INDEX IF NOT EXISTS idx_rce_merchant   ON recovery_case_events(merchant_id);
CREATE INDEX IF NOT EXISTS idx_rce_type       ON recovery_case_events(event_type);
CREATE INDEX IF NOT EXISTS idx_rce_occurred   ON recovery_case_events(occurred_at DESC);

-- ---------------------------------------------------------------------------
-- checkout_sessions: checkout abandonment recovery
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS checkout_sessions (
    session_id        TEXT    PRIMARY KEY,
    merchant_id       TEXT    NOT NULL,
    customer_ref      TEXT,
    customer_email    TEXT,
    customer_phone    TEXT,
    amount            REAL    NOT NULL DEFAULT 0,
    currency          TEXT    NOT NULL DEFAULT 'INR',
    payment_method    TEXT,
    stage_reached     TEXT    NOT NULL DEFAULT 'initiated',
    abandoned_at      TEXT,
    time_to_abandon_s INTEGER,
    recovery_case_id  TEXT,
    status            TEXT    NOT NULL DEFAULT 'abandoned',
    recovery_link     TEXT,
    recovery_link_expires_at TEXT,
    recovered_at      TEXT,
    source            TEXT    NOT NULL DEFAULT 'SIMULATED',
    metadata          TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    is_demo           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cs_merchant   ON checkout_sessions(merchant_id);
CREATE INDEX IF NOT EXISTS idx_cs_status     ON checkout_sessions(status);
CREATE INDEX IF NOT EXISTS idx_cs_created    ON checkout_sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cs_demo       ON checkout_sessions(is_demo);

-- ---------------------------------------------------------------------------
-- b2b_invoices: B2B receivables chaser
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS b2b_invoices (
    invoice_id        TEXT    PRIMARY KEY,
    merchant_id       TEXT    NOT NULL,
    customer_name     TEXT    NOT NULL,
    customer_email    TEXT,
    customer_phone    TEXT,
    customer_company  TEXT,
    invoice_number    TEXT,
    amount            REAL    NOT NULL DEFAULT 0,
    currency          TEXT    NOT NULL DEFAULT 'INR',
    issued_at         TEXT    NOT NULL,
    due_at            TEXT    NOT NULL,
    overdue_days      INTEGER NOT NULL DEFAULT 0,
    status            TEXT    NOT NULL DEFAULT 'due',
    priority          TEXT    NOT NULL DEFAULT 'medium',
    recovery_case_id  TEXT,
    last_reminder_at  TEXT,
    reminder_count    INTEGER NOT NULL DEFAULT 0,
    promised_payment_date TEXT,
    promise_id        TEXT,
    paid_at           TEXT,
    paid_amount       REAL    DEFAULT 0,
    notes             TEXT,
    source            TEXT    NOT NULL DEFAULT 'SIMULATED',
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    is_demo           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bi_merchant   ON b2b_invoices(merchant_id);
CREATE INDEX IF NOT EXISTS idx_bi_status     ON b2b_invoices(status);
CREATE INDEX IF NOT EXISTS idx_bi_due        ON b2b_invoices(due_at);
CREATE INDEX IF NOT EXISTS idx_bi_demo       ON b2b_invoices(is_demo);

-- ---------------------------------------------------------------------------
-- promises: Promise-to-pay tracker
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS promises (
    promise_id        TEXT    PRIMARY KEY,
    merchant_id       TEXT    NOT NULL,
    case_id           TEXT,
    invoice_id        TEXT,
    customer_ref      TEXT,
    customer_name     TEXT,
    customer_email    TEXT,
    promised_amount   REAL    NOT NULL DEFAULT 0,
    promised_date     TEXT    NOT NULL,
    source            TEXT    NOT NULL DEFAULT 'manual',
    confidence        TEXT    NOT NULL DEFAULT 'medium',
    status            TEXT    NOT NULL DEFAULT 'upcoming',
    follow_up_date    TEXT,
    actual_paid_amount REAL   DEFAULT 0,
    paid_at           TEXT,
    missed_at         TEXT,
    escalated_at      TEXT,
    notes             TEXT,
    data_source       TEXT    NOT NULL DEFAULT 'SIMULATED',
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    is_demo           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_prom_merchant  ON promises(merchant_id);
CREATE INDEX IF NOT EXISTS idx_prom_status    ON promises(status);
CREATE INDEX IF NOT EXISTS idx_prom_date      ON promises(promised_date);
CREATE INDEX IF NOT EXISTS idx_prom_demo      ON promises(is_demo);

-- ---------------------------------------------------------------------------
-- recovery_actions: every action taken on a recovery case
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recovery_actions (
    action_id         TEXT    PRIMARY KEY,
    case_id           TEXT    NOT NULL,
    merchant_id       TEXT    NOT NULL,
    action_type       TEXT    NOT NULL,
    channel           TEXT,
    language          TEXT    NOT NULL DEFAULT 'en',
    message_preview   TEXT,
    expected_value    REAL,
    actual_outcome    TEXT,
    executed_by       TEXT    NOT NULL DEFAULT 'system',
    execution_mode    TEXT    NOT NULL DEFAULT 'SIMULATED',
    scheduled_at      TEXT,
    executed_at       TEXT,
    result_details    TEXT,
    idempotency_key   TEXT    UNIQUE,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_ra_case        ON recovery_actions(case_id);
CREATE INDEX IF NOT EXISTS idx_ra_merchant    ON recovery_actions(merchant_id);
CREATE INDEX IF NOT EXISTS idx_ra_type        ON recovery_actions(action_type);
CREATE INDEX IF NOT EXISTS idx_ra_created     ON recovery_actions(created_at DESC);

-- ---------------------------------------------------------------------------
-- channel_decisions: channel selection audit trail
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS channel_decisions (
    decision_id       TEXT    PRIMARY KEY,
    case_id           TEXT    NOT NULL,
    merchant_id       TEXT    NOT NULL,
    selected_channel  TEXT    NOT NULL,
    rationale         TEXT,
    expected_recovery_prob REAL,
    expected_net_value REAL,
    channels_considered TEXT,
    policy_applied    TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_cd_case      ON channel_decisions(case_id);
CREATE INDEX IF NOT EXISTS idx_cd_merchant  ON channel_decisions(merchant_id);

-- ---------------------------------------------------------------------------
-- voice_scripts: voice-ready recovery scripts (no actual calls)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voice_scripts (
    script_id         TEXT    PRIMARY KEY,
    case_id           TEXT    NOT NULL,
    merchant_id       TEXT    NOT NULL,
    language          TEXT    NOT NULL DEFAULT 'en',
    script_text       TEXT    NOT NULL,
    call_intent       TEXT    NOT NULL DEFAULT 'recovery_reminder',
    status            TEXT    NOT NULL DEFAULT 'READY_FOR_PROVIDER',
    simulated_outcome TEXT,
    provider_adapter  TEXT,
    notes             TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_vs_case     ON voice_scripts(case_id);
CREATE INDEX IF NOT EXISTS idx_vs_merchant ON voice_scripts(merchant_id);

-- ---------------------------------------------------------------------------
-- demo_snapshots
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS demo_snapshots (
    snapshot_id       TEXT    PRIMARY KEY,
    scenario_name     TEXT    NOT NULL,
    step_number       INTEGER NOT NULL DEFAULT 1,
    step_description  TEXT,
    payload           TEXT    NOT NULL,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','utc'))
);

-- ---------------------------------------------------------------------------
-- mandate_retry_log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mandate_retry_log (
    retry_id          TEXT    PRIMARY KEY,
    case_id           TEXT,
    mandate_customer_id TEXT,
    merchant_id       TEXT    NOT NULL,
    attempt_number    INTEGER NOT NULL DEFAULT 1,
    scheduled_at      TEXT    NOT NULL,
    executed_at       TEXT,
    failure_reason    TEXT,
    retry_reason      TEXT,
    no_retry_reason   TEXT,
    channel           TEXT,
    outcome           TEXT,
    execution_mode    TEXT    NOT NULL DEFAULT 'SIMULATED',
    expected_value    REAL,
    amount            REAL,
    decision_signals  TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_mrl_case     ON mandate_retry_log(case_id);
CREATE INDEX IF NOT EXISTS idx_mrl_merchant ON mandate_retry_log(merchant_id);
CREATE INDEX IF NOT EXISTS idx_mrl_cid      ON mandate_retry_log(mandate_customer_id);

-- ---------------------------------------------------------------------------
-- payment_degradation_events
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payment_degradation_events (
    event_id          TEXT    PRIMARY KEY,
    merchant_id       TEXT    NOT NULL,
    detected_at       TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    degradation_type  TEXT    NOT NULL,
    affected_segment  TEXT    NOT NULL,
    severity          TEXT    NOT NULL DEFAULT 'warning',
    what_changed      TEXT    NOT NULL,
    why_likely        TEXT,
    revenue_at_risk   REAL    DEFAULT 0,
    affected_cases    INTEGER DEFAULT 0,
    recommended_action TEXT,
    confidence        REAL    DEFAULT 0.5,
    evidence          TEXT,
    resolved_at       TEXT,
    status            TEXT    NOT NULL DEFAULT 'active',
    data_type         TEXT    NOT NULL DEFAULT 'actual',
    is_demo           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pde_merchant ON payment_degradation_events(merchant_id);
CREATE INDEX IF NOT EXISTS idx_pde_status   ON payment_degradation_events(status);
CREATE INDEX IF NOT EXISTS idx_pde_detected ON payment_degradation_events(detected_at DESC);

-- ---------------------------------------------------------------------------
-- merchant_recovery_policies
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS merchant_recovery_policies (
    policy_id         TEXT    PRIMARY KEY,
    merchant_id       TEXT    NOT NULL UNIQUE,
    max_retries       INTEGER NOT NULL DEFAULT 3,
    retry_cooldown_hours INTEGER NOT NULL DEFAULT 24,
    max_messages_per_week INTEGER NOT NULL DEFAULT 3,
    preferred_channel TEXT    NOT NULL DEFAULT 'email',
    preferred_language TEXT   NOT NULL DEFAULT 'en',
    working_hours_start INTEGER NOT NULL DEFAULT 9,
    working_hours_end   INTEGER NOT NULL DEFAULT 20,
    min_expected_value_rs REAL NOT NULL DEFAULT 0,
    approval_threshold_rs REAL NOT NULL DEFAULT 10000,
    escalation_overdue_days INTEGER NOT NULL DEFAULT 30,
    escalation_amount_rs REAL NOT NULL DEFAULT 50000,
    checkout_recovery_enabled INTEGER NOT NULL DEFAULT 1,
    b2b_recovery_enabled      INTEGER NOT NULL DEFAULT 1,
    voice_recovery_enabled    INTEGER NOT NULL DEFAULT 0,
    version           INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_mrp_merchant ON merchant_recovery_policies(merchant_id);

-- ---------------------------------------------------------------------------
-- approval_requests
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS approval_requests (
    request_id        TEXT    PRIMARY KEY,
    merchant_id       TEXT    NOT NULL,
    case_id           TEXT,
    action_type       TEXT    NOT NULL,
    title             TEXT    NOT NULL,
    description       TEXT    NOT NULL,
    recommended_action TEXT,
    expected_value    REAL,
    status            TEXT    NOT NULL DEFAULT 'pending',
    expires_at        TEXT,
    decided_by        TEXT,
    decided_at        TEXT,
    decision_notes    TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_ar_merchant  ON approval_requests(merchant_id);
CREATE INDEX IF NOT EXISTS idx_ar_status    ON approval_requests(status);
CREATE INDEX IF NOT EXISTS idx_ar_created   ON approval_requests(created_at DESC);
"""


def init_phase7(conn=None) -> None:
    """Create all Phase 7 tables (idempotent via CREATE IF NOT EXISTS)."""
    import db as _db
    own = conn is None
    if own:
        conn = _db.get_connection()
    try:
        conn.executescript(PHASE7_SCHEMA)
        _migrate_phase7(conn)
        conn.commit()
    finally:
        if own:
            conn.close()


def _migrate_phase7(conn) -> None:
    """Add merchant_id / scenario_type columns to mandate_failures if missing."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(mandate_failures)")}
    for col, decl in {"merchant_id": "TEXT", "scenario_type": "TEXT NOT NULL DEFAULT 'mandate_retry'"}.items():
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE mandate_failures ADD COLUMN {col} {decl}")
            except Exception:
                pass


def reset_phase7(conn) -> None:
    """Clear all Phase 7 tables (used by reset_db and demo reset)."""
    for tbl in (
        "approval_requests", "merchant_recovery_policies",
        "payment_degradation_events", "mandate_retry_log",
        "demo_snapshots", "voice_scripts", "channel_decisions",
        "recovery_actions", "promises", "b2b_invoices",
        "checkout_sessions", "recovery_case_events", "recovery_cases",
    ):
        try:
            conn.execute(f"DELETE FROM {tbl}")
        except Exception:
            pass
