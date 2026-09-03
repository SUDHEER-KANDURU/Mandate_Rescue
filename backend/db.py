"""SQLite data-access layer for Mandate Rescue.

Thin, transparent wrapper over sqlite3 with parameterized SQL (no ORM), so every
row that backs a dashboard number is easy to audit. See design.md section 3.
"""

import os
import sqlite3

# Keep the database at the project root (one level up from backend/) so it stays a
# single canonical file regardless of which backend module is the entrypoint.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_PROJECT_ROOT, "mandate_rescue.db")


def get_connection():
    """Return a SQLite connection with row access by column name and FK enforcement.

    A busy timeout is set so that brief write contention (e.g. a live agent run
    streaming writes while another request touches the same DB) waits and retries
    instead of failing immediately with "database is locked". Without this, a
    concurrent /api/reset during an in-flight run could 500.
    """
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def get_memory_connection():
    """Return an isolated in-memory SQLite connection (same row/PRAGMA setup).

    Used by the Policy Experimentation Sandbox so repeated simulation runs never touch
    the on-disk `mandate_rescue.db`. The database exists only for the lifetime of the
    returned connection; the caller is responsible for init_db() + seeding + closing.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# --- Schema -----------------------------------------------------------------
# The mandate_failures schema carries the core fields plus the R13-R16 realism/
# compliance columns so the schema is stable. Behavior for those columns lands
# in the Phase 3 agent tasks; here they are just storage.

SCHEMA = """
CREATE TABLE IF NOT EXISTS mandate_failures (
    customer_id               TEXT PRIMARY KEY,
    amount                    REAL    NOT NULL,
    failure_reason            TEXT    NOT NULL,
    failure_date              TEXT    NOT NULL,
    past_retry_count          INTEGER NOT NULL DEFAULT 0,
    customer_tenure_months    INTEGER NOT NULL,
    past_payment_success_rate REAL    NOT NULL,
    merchant_category         TEXT    NOT NULL,
    case_status               TEXT    NOT NULL DEFAULT 'new',
    raw_event_type            TEXT,
    mandate_limit             REAL    NOT NULL DEFAULT 5000,
    compliance_status         TEXT,
    dunning_stage             INTEGER NOT NULL DEFAULT 0,
    health_score              REAL,
    history_success_days      TEXT,
    webhook_signature         TEXT,
    source                    TEXT    NOT NULL DEFAULT 'synthetic'
);

CREATE TABLE IF NOT EXISTS audit_log (
    event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id       TEXT    NOT NULL,
    event_timestamp   TEXT    NOT NULL,
    event_type        TEXT    NOT NULL,
    action_taken      TEXT    NOT NULL,
    outcome           TEXT    NOT NULL,
    attempt_number    INTEGER NOT NULL DEFAULT 0,
    reasoning_text    TEXT    NOT NULL,
    case_status_after TEXT    NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES mandate_failures(customer_id)
);

CREATE INDEX IF NOT EXISTS idx_audit_customer ON audit_log(customer_id);
CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_event_id_desc ON audit_log(event_id DESC);

CREATE TABLE IF NOT EXISTS webhook_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    razorpay_event_id TEXT    UNIQUE NOT NULL,
    payload_hash      TEXT,
    received_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    processed         INTEGER NOT NULL DEFAULT 0,
    customer_id       TEXT,
    event_type        TEXT,
    rejected_reason   TEXT
);

CREATE INDEX IF NOT EXISTS idx_webhook_events_event_id ON webhook_events(razorpay_event_id);

CREATE TABLE IF NOT EXISTS state_transitions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id   TEXT    NOT NULL,
    from_status   TEXT    NOT NULL,
    to_status     TEXT    NOT NULL,
    transitioned_at TEXT  NOT NULL,
    triggered_by  TEXT    NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES mandate_failures(customer_id)
);

CREATE INDEX IF NOT EXISTS idx_state_transitions_customer ON state_transitions(customer_id);

CREATE TABLE IF NOT EXISTS recovery_jobs (
    job_id            TEXT    PRIMARY KEY,           -- UUID, stable across restarts
    customer_id       TEXT    NOT NULL,
    attempt_number    INTEGER NOT NULL DEFAULT 1,    -- 1-indexed; max = agent.MAX_RETRIES
    execution_mode    TEXT    NOT NULL DEFAULT 'simulation',  -- 'real_test' | 'simulation'
    status            TEXT    NOT NULL DEFAULT 'scheduled',
        -- scheduled | claimed | executing | succeeded | failed | cancelled | exhausted
    scheduled_at      TEXT    NOT NULL,              -- ISO-8601 UTC; when job becomes due
    claimed_at        TEXT,                          -- set when a worker picks it up
    executed_at       TEXT,                          -- set when execution attempt completes
    outcome           TEXT,                          -- ExecutionOutcome value
    razorpay_payment_id        TEXT,
    razorpay_subscription_id   TEXT,
    razorpay_payment_link_id   TEXT,
    payment_link_url           TEXT,
    amount_rupees     REAL,
    failure_reason    TEXT,                          -- error text if status=failed
    retry_count       INTEGER NOT NULL DEFAULT 0,    -- worker-level retries (transient errors)
    max_retries       INTEGER NOT NULL DEFAULT 3,    -- policy cap
    idempotency_key   TEXT    UNIQUE NOT NULL,       -- customer_id + ':' + attempt_number
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    FOREIGN KEY (customer_id) REFERENCES mandate_failures(customer_id)
);

CREATE INDEX IF NOT EXISTS idx_recovery_jobs_customer ON recovery_jobs(customer_id);
CREATE INDEX IF NOT EXISTS idx_recovery_jobs_status ON recovery_jobs(status);
CREATE INDEX IF NOT EXISTS idx_recovery_jobs_scheduled ON recovery_jobs(scheduled_at)
    WHERE status = 'scheduled';
"""


def init_db(conn=None):
    """Create tables and indexes if they do not exist, then apply light migrations."""
    own = conn is None
    if own:
        conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        if own:
            conn.close()


def _migrate(conn):
    """Additive migrations for pre-existing databases (CREATE IF NOT EXISTS can't
    add columns to an already-created table). Adds any missing whitelisted columns.
    Also creates new tables added in later schema revisions."""
    # mandate_failures column additions
    existing_mf = {row[1] for row in conn.execute("PRAGMA table_info(mandate_failures)")}
    additive_mf = {
        "webhook_signature": "TEXT",
        "source": "TEXT NOT NULL DEFAULT 'synthetic'",
    }
    for col, decl in additive_mf.items():
        if col not in existing_mf:
            conn.execute(f"ALTER TABLE mandate_failures ADD COLUMN {col} {decl}")

    # webhook_events column additions (added in schema revision 2)
    existing_we = {row[1] for row in conn.execute("PRAGMA table_info(webhook_events)")}
    additive_we = {
        "customer_id": "TEXT",
        "event_type": "TEXT",
        "rejected_reason": "TEXT",
    }
    for col, decl in additive_we.items():
        if col not in existing_we:
            conn.execute(f"ALTER TABLE webhook_events ADD COLUMN {col} {decl}")

    # Create state_transitions table if not present (added in schema revision 3).
    # NOTE: executescript() commits any open transaction — we call it only once here,
    # after all ALTER TABLE column additions above, so the indexes below can safely
    # reference the just-added columns.  idx_webhook_events_customer depends on the
    # customer_id column we may have just added, so it lives in this block.
    conn.executescript("""
CREATE TABLE IF NOT EXISTS state_transitions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id   TEXT    NOT NULL,
    from_status   TEXT    NOT NULL,
    to_status     TEXT    NOT NULL,
    transitioned_at TEXT  NOT NULL,
    triggered_by  TEXT    NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES mandate_failures(customer_id)
);
CREATE INDEX IF NOT EXISTS idx_state_transitions_customer ON state_transitions(customer_id);
""")
    # Add the webhook_events customer index via execute() (not executescript) so it
    # runs inside the current transaction without committing the ALTERs above first.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_webhook_events_customer "
        "ON webhook_events(customer_id)"
    )
    # Phase 5: indexes on mandate_failures for common filter/aggregation patterns.
    # These were missing and caused full table scans on every query.py, metrics.py,
    # and /api/cases call that filters by failure_reason, merchant_category, or status.
    conn.executescript("""
CREATE INDEX IF NOT EXISTS idx_mf_case_status      ON mandate_failures(case_status);
CREATE INDEX IF NOT EXISTS idx_mf_failure_reason   ON mandate_failures(failure_reason);
CREATE INDEX IF NOT EXISTS idx_mf_merchant_category ON mandate_failures(merchant_category);
CREATE INDEX IF NOT EXISTS idx_mf_amount           ON mandate_failures(amount);
CREATE INDEX IF NOT EXISTS idx_mf_failure_date     ON mandate_failures(failure_date);
CREATE INDEX IF NOT EXISTS idx_audit_event_id_desc ON audit_log(event_id DESC);
""")
    # Phase 4: recovery_jobs table and indexes (added in schema revision 4).
    conn.executescript("""
CREATE TABLE IF NOT EXISTS recovery_jobs (
    job_id            TEXT    PRIMARY KEY,
    customer_id       TEXT    NOT NULL,
    attempt_number    INTEGER NOT NULL DEFAULT 1,
    execution_mode    TEXT    NOT NULL DEFAULT 'simulation',
    status            TEXT    NOT NULL DEFAULT 'scheduled',
    scheduled_at      TEXT    NOT NULL,
    claimed_at        TEXT,
    executed_at       TEXT,
    outcome           TEXT,
    razorpay_payment_id        TEXT,
    razorpay_subscription_id   TEXT,
    razorpay_payment_link_id   TEXT,
    payment_link_url           TEXT,
    amount_rupees     REAL,
    failure_reason    TEXT,
    retry_count       INTEGER NOT NULL DEFAULT 0,
    max_retries       INTEGER NOT NULL DEFAULT 3,
    idempotency_key   TEXT    UNIQUE NOT NULL,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    FOREIGN KEY (customer_id) REFERENCES mandate_failures(customer_id)
);
CREATE INDEX IF NOT EXISTS idx_recovery_jobs_customer  ON recovery_jobs(customer_id);
CREATE INDEX IF NOT EXISTS idx_recovery_jobs_status    ON recovery_jobs(status);
""")
    # Phase 6: Closed-loop learning tables.
    conn.executescript("""
-- strategy_performance: durable, per-dimension strategy stats with data provenance.
-- Each row is one (strategy, dimension_key, dimension_value) bucket.
-- provenance: REAL_TEST | SIMULATION | HISTORICAL | ESTIMATE | FORECAST
CREATE TABLE IF NOT EXISTS strategy_performance (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy          TEXT    NOT NULL,
    dimension_key     TEXT    NOT NULL,   -- e.g. 'failure_reason', 'merchant_category', 'global'
    dimension_value   TEXT    NOT NULL,   -- e.g. 'insufficient_funds', 'subscription', 'all'
    attempts          INTEGER NOT NULL DEFAULT 0,
    recoveries        INTEGER NOT NULL DEFAULT 0,
    amount_recovered  REAL    NOT NULL DEFAULT 0.0,
    amount_attempted  REAL    NOT NULL DEFAULT 0.0,
    escalations       INTEGER NOT NULL DEFAULT 0,
    time_to_recovery_sum_hours REAL NOT NULL DEFAULT 0.0,
    provenance        TEXT    NOT NULL DEFAULT 'HISTORICAL',
    last_updated      TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    UNIQUE (strategy, dimension_key, dimension_value, provenance)
);
CREATE INDEX IF NOT EXISTS idx_sp_strategy ON strategy_performance(strategy);
CREATE INDEX IF NOT EXISTS idx_sp_dimension ON strategy_performance(dimension_key, dimension_value);

-- experiments: controlled A/B strategy experiments.
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id     TEXT    PRIMARY KEY,
    name              TEXT    NOT NULL,
    description       TEXT,
    merchant_category TEXT,
    failure_reason    TEXT,
    control_strategy  TEXT    NOT NULL,
    treatment_strategy TEXT   NOT NULL,
    cohort_definition TEXT    NOT NULL,   -- JSON describing assignment criteria
    status            TEXT    NOT NULL DEFAULT 'active',
        -- active | completed | cancelled | paused
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    started_at        TEXT,
    ended_at          TEXT,
    created_by        TEXT    NOT NULL DEFAULT 'system',
    min_sample_size   INTEGER NOT NULL DEFAULT 30,
    notes             TEXT
);
CREATE INDEX IF NOT EXISTS idx_exp_status ON experiments(status);
CREATE INDEX IF NOT EXISTS idx_exp_created ON experiments(created_at);

-- experiment_assignments: which case was assigned to which arm.
CREATE TABLE IF NOT EXISTS experiment_assignments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id     TEXT    NOT NULL,
    customer_id       TEXT    NOT NULL,
    arm               TEXT    NOT NULL,   -- 'control' | 'treatment'
    assigned_at       TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    UNIQUE (experiment_id, customer_id),
    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id),
    FOREIGN KEY (customer_id) REFERENCES mandate_failures(customer_id)
);
CREATE INDEX IF NOT EXISTS idx_ea_experiment ON experiment_assignments(experiment_id);
CREATE INDEX IF NOT EXISTS idx_ea_customer ON experiment_assignments(customer_id);

-- experiment_outcomes: final outcome for each assigned case (written once, immutable).
CREATE TABLE IF NOT EXISTS experiment_outcomes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id     TEXT    NOT NULL,
    customer_id       TEXT    NOT NULL,
    arm               TEXT    NOT NULL,
    strategy_used     TEXT    NOT NULL,
    outcome_status    TEXT    NOT NULL,   -- 'recovered' | 'escalated' | 'in_progress' | ...
    amount_rupees     REAL    NOT NULL DEFAULT 0.0,
    recovered         INTEGER NOT NULL DEFAULT 0,   -- 1 or 0
    time_to_recovery_hours REAL,
    execution_mode    TEXT    NOT NULL DEFAULT 'simulation',
    recorded_at       TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    UNIQUE (experiment_id, customer_id),
    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id)
);
CREATE INDEX IF NOT EXISTS idx_eo_experiment ON experiment_outcomes(experiment_id);

-- policy_versions: immutable record of every policy version.
-- Once created, a policy_version row must never be modified (except status transitions).
CREATE TABLE IF NOT EXISTS policy_versions (
    version_id        TEXT    PRIMARY KEY,
    version_number    INTEGER NOT NULL,
    merchant_category TEXT    NOT NULL DEFAULT 'all',
    strategy_params   TEXT    NOT NULL,   -- JSON: full strategy parameter set
    status            TEXT    NOT NULL DEFAULT 'draft',
        -- draft | recommended | under_review | approved | active | deprecated | rolled_back
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    created_by        TEXT    NOT NULL DEFAULT 'system',
    activated_at      TEXT,
    deprecated_at     TEXT,
    reason            TEXT,              -- human-readable reason for this version
    evidence_summary  TEXT,             -- JSON: evidence that motivated this version
    approved_by       TEXT,
    approved_at       TEXT,
    previous_version_id TEXT,
    expected_impact   TEXT              -- JSON: {recovery_rate_delta, amount_delta, confidence}
);
CREATE INDEX IF NOT EXISTS idx_pv_status ON policy_versions(status);
CREATE INDEX IF NOT EXISTS idx_pv_merchant ON policy_versions(merchant_category);
CREATE INDEX IF NOT EXISTS idx_pv_version_number ON policy_versions(version_number DESC);

-- policy_performance: measured performance of a policy version after activation.
-- Separate from policy_versions so historical policy rows stay immutable.
CREATE TABLE IF NOT EXISTS policy_performance (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id        TEXT    NOT NULL,
    measurement_window_days INTEGER NOT NULL DEFAULT 30,
    cases_observed    INTEGER NOT NULL DEFAULT 0,
    recoveries        INTEGER NOT NULL DEFAULT 0,
    recovery_rate     REAL    NOT NULL DEFAULT 0.0,
    amount_recovered  REAL    NOT NULL DEFAULT 0.0,
    escalation_rate   REAL    NOT NULL DEFAULT 0.0,
    measured_at       TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    data_type         TEXT    NOT NULL DEFAULT 'actual',
    FOREIGN KEY (version_id) REFERENCES policy_versions(version_id)
);
CREATE INDEX IF NOT EXISTS idx_pp_version ON policy_performance(version_id);

-- policy_recommendations: each data-backed recommendation surfaced to the merchant.
CREATE TABLE IF NOT EXISTS policy_recommendations (
    recommendation_id TEXT    PRIMARY KEY,
    title             TEXT    NOT NULL,
    what_changes      TEXT    NOT NULL,   -- human-readable description of the change
    why_evidence      TEXT    NOT NULL,   -- JSON: evidence trail
    current_strategy  TEXT    NOT NULL,
    recommended_strategy TEXT NOT NULL,
    current_rate      REAL,
    recommended_rate  REAL,
    sample_size       INTEGER NOT NULL DEFAULT 0,
    estimated_incremental_rs REAL,
    confidence        TEXT    NOT NULL DEFAULT 'low',
        -- low | moderate | high
    data_source       TEXT    NOT NULL DEFAULT 'simulation',
        -- REAL_TEST | SIMULATION | HISTORICAL | MIXED
    status            TEXT    NOT NULL DEFAULT 'draft',
        -- draft | recommended | under_review | approved | rejected | superseded
    created_at        TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    reviewed_at       TEXT,
    reviewed_by       TEXT,
    approved_at       TEXT,
    approved_by       TEXT,
    rejected_at       TEXT,
    rejected_by       TEXT,
    rejection_reason  TEXT,
    merchant_category TEXT    NOT NULL DEFAULT 'all',
    failure_reason    TEXT,
    experiment_id     TEXT,              -- if backed by a specific experiment
    policy_version_id TEXT              -- version created when approved
);
CREATE INDEX IF NOT EXISTS idx_pr_status ON policy_recommendations(status);
CREATE INDEX IF NOT EXISTS idx_pr_created ON policy_recommendations(created_at DESC);

-- policy_audit_log: append-only record of every policy governance action.
CREATE TABLE IF NOT EXISTS policy_audit_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type       TEXT    NOT NULL,
        -- version_created | version_approved | version_activated | version_deprecated
        -- | version_rolled_back | recommendation_created | recommendation_approved
        -- | recommendation_rejected
    version_id        TEXT,
    recommendation_id TEXT,
    actor             TEXT    NOT NULL DEFAULT 'system',
    previous_status   TEXT,
    new_status        TEXT,
    notes             TEXT,
    action_at         TEXT    NOT NULL DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_pal_version ON policy_audit_log(version_id);
CREATE INDEX IF NOT EXISTS idx_pal_action ON policy_audit_log(action_type);
CREATE INDEX IF NOT EXISTS idx_pal_at ON policy_audit_log(action_at DESC);
""")
    # Additive column migrations for recovery_jobs (future-proofing).
    existing_rj = {row[1] for row in conn.execute("PRAGMA table_info(recovery_jobs)")}
    additive_rj = {
        "razorpay_subscription_id": "TEXT",
        "payment_link_url": "TEXT",
        "retry_count": "INTEGER NOT NULL DEFAULT 0",
        "max_retries": "INTEGER NOT NULL DEFAULT 3",
    }
    for col, decl in additive_rj.items():
        if col not in existing_rj:
            try:
                conn.execute(f"ALTER TABLE recovery_jobs ADD COLUMN {col} {decl}")
            except Exception:
                pass  # column may already exist in some DB files
    # Phase 6: additive column migrations for new tables
    _migrate_phase6(conn)


def reset_db(conn=None):
    """Drop all rows so a fresh seed/run is reproducible. Keeps schema."""
    own = conn is None
    if own:
        conn = get_connection()
    try:
        init_db(conn)
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM state_transitions")
        conn.execute("DELETE FROM mandate_failures")
        conn.execute("DELETE FROM webhook_events")
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'audit_log'")
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'webhook_events'")
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'state_transitions'")
        # Phase 4: also clear recovery jobs so a reset starts with an empty job queue.
        try:
            conn.execute("DELETE FROM recovery_jobs")
        except Exception:
            pass  # table may not exist yet on very old DB files
        # Phase 6: clear closed-loop learning tables on reset.
        for tbl in (
            "policy_audit_log", "policy_performance", "policy_recommendations",
            "policy_versions", "experiment_outcomes", "experiment_assignments",
            "experiments", "strategy_performance",
        ):
            try:
                conn.execute(f"DELETE FROM {tbl}")
            except Exception:
                pass
        conn.commit()
    finally:
        if own:
            conn.close()


# --- Query helpers ----------------------------------------------------------

MANDATE_COLUMNS = [
    "customer_id", "amount", "failure_reason", "failure_date", "past_retry_count",
    "customer_tenure_months", "past_payment_success_rate", "merchant_category",
    "case_status", "raw_event_type", "mandate_limit", "compliance_status",
    "dunning_stage", "health_score", "history_success_days", "webhook_signature",
    "source",
]


def insert_mandate_failure(conn, record):
    """Insert one mandate_failures row from a dict keyed by column name."""
    cols = [c for c in MANDATE_COLUMNS if c in record]
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT INTO mandate_failures ({', '.join(cols)}) VALUES ({placeholders})"
    conn.execute(sql, [record[c] for c in cols])


def get_all_cases(conn):
    """Return all mandate_failures rows as dicts."""
    rows = conn.execute("SELECT * FROM mandate_failures").fetchall()
    return [dict(r) for r in rows]


def get_case(conn, customer_id):
    row = conn.execute(
        "SELECT * FROM mandate_failures WHERE customer_id = ?", (customer_id,)
    ).fetchone()
    return dict(row) if row else None


def update_case(conn, customer_id, **fields):
    """Update arbitrary columns on a case; only whitelisted columns are allowed."""
    allowed = {k: v for k, v in fields.items() if k in MANDATE_COLUMNS}
    if not allowed:
        return
    assignments = ", ".join(f"{k} = ?" for k in allowed)
    values = list(allowed.values()) + [customer_id]
    conn.execute(
        f"UPDATE mandate_failures SET {assignments} WHERE customer_id = ?", values
    )


def insert_audit(conn, customer_id, event_timestamp, event_type, action_taken,
                 outcome, attempt_number, reasoning_text, case_status_after):
    """Append one audit_log row. Every agent action must call this."""
    conn.execute(
        """
        INSERT INTO audit_log (
            customer_id, event_timestamp, event_type, action_taken,
            outcome, attempt_number, reasoning_text, case_status_after
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (customer_id, event_timestamp, event_type, action_taken,
         outcome, attempt_number, reasoning_text, case_status_after),
    )


def get_audit_for_case(conn, customer_id):
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE customer_id = ? ORDER BY event_id",
        (customer_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_audit(conn):
    rows = conn.execute("SELECT * FROM audit_log ORDER BY event_id").fetchall()
    return [dict(r) for r in rows]


def get_webhook_event(conn, razorpay_event_id):
    """Return the webhook_events row for this Razorpay event id, or None."""
    row = conn.execute(
        "SELECT * FROM webhook_events WHERE razorpay_event_id = ?",
        (razorpay_event_id,),
    ).fetchone()
    return dict(row) if row else None


def insert_webhook_event(conn, razorpay_event_id, payload_hash=None):
    """Insert a webhook_events row. Returns True on insert, False if the event id
    already exists (UNIQUE constraint — the duplicate must not be treated as new)."""
    try:
        conn.execute(
            """
            INSERT INTO webhook_events (razorpay_event_id, payload_hash, processed)
            VALUES (?, ?, 0)
            """,
            (razorpay_event_id, payload_hash),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def mark_webhook_event_processed(conn, razorpay_event_id):
    conn.execute(
        "UPDATE webhook_events SET processed = 1 WHERE razorpay_event_id = ?",
        (razorpay_event_id,),
    )


# --- State transition helpers -----------------------------------------------

# Explicit legal status transitions. A transition not in this map is illegal and
# must be rejected. 'new' is the initial state; terminal states have no outbound
# transitions (except for webhook_duplicate handling which reads but never transitions).
LEGAL_TRANSITIONS = {
    "new": frozenset({"in_progress", "rejected", "invalid"}),
    "in_progress": frozenset({"recovered", "escalated", "promised", "broken_promise", "in_progress"}),
    "promised": frozenset({"recovered", "broken_promise", "in_progress"}),
    "broken_promise": frozenset({"recovered", "escalated", "in_progress"}),
    # Terminal states — no outbound transitions permitted.
    "recovered": frozenset(),
    "escalated": frozenset(),
    "rejected": frozenset(),
    "invalid": frozenset(),
}


def is_legal_transition(from_status, to_status):
    """Return True if the transition from_status -> to_status is in the legal set."""
    return to_status in LEGAL_TRANSITIONS.get(from_status, frozenset())


def record_state_transition(conn, customer_id, from_status, to_status, triggered_by):
    """Append a row to state_transitions. Only call after verifying legality."""
    from datetime import datetime
    conn.execute(
        """
        INSERT INTO state_transitions
            (customer_id, from_status, to_status, transitioned_at, triggered_by)
        VALUES (?, ?, ?, ?, ?)
        """,
        (customer_id, from_status, to_status,
         datetime.now().isoformat(timespec="seconds"), triggered_by),
    )


def get_state_transitions(conn, customer_id):
    """Return all state_transitions rows for a case in chronological order."""
    rows = conn.execute(
        "SELECT * FROM state_transitions WHERE customer_id = ? ORDER BY id",
        (customer_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Phase 4: Recovery job queue helpers
# ---------------------------------------------------------------------------

RECOVERY_JOB_COLUMNS = [
    "job_id", "customer_id", "attempt_number", "execution_mode", "status",
    "scheduled_at", "claimed_at", "executed_at", "outcome",
    "razorpay_payment_id", "razorpay_subscription_id",
    "razorpay_payment_link_id", "payment_link_url",
    "amount_rupees", "failure_reason", "retry_count", "max_retries",
    "idempotency_key", "created_at",
]

# Valid job status values — enforced by the helper functions.
JOB_STATUS_SCHEDULED  = "scheduled"
JOB_STATUS_CLAIMED    = "claimed"
JOB_STATUS_EXECUTING  = "executing"
JOB_STATUS_SUCCEEDED  = "succeeded"
JOB_STATUS_FAILED     = "failed"
JOB_STATUS_CANCELLED  = "cancelled"
JOB_STATUS_EXHAUSTED  = "exhausted"   # retry cap reached

_TERMINAL_JOB_STATUSES = frozenset({
    JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED, JOB_STATUS_EXHAUSTED,
})


def create_recovery_job(conn, job_id: str, customer_id: str, attempt_number: int,
                        execution_mode: str, scheduled_at: str,
                        max_retries: int = 3,
                        razorpay_subscription_id: str = None,
                        razorpay_payment_id: str = None,
                        amount_rupees: float = None) -> bool:
    """Insert a new recovery job. Returns True on insert, False if idempotency_key
    already exists (same customer + attempt_number already scheduled).

    The idempotency_key = customer_id + ':' + str(attempt_number) prevents the
    scheduler from queuing the same logical attempt twice even if the agent runs
    multiple times or the application restarts.
    """
    idem_key = f"{customer_id}:{attempt_number}"
    try:
        conn.execute(
            """
            INSERT INTO recovery_jobs (
                job_id, customer_id, attempt_number, execution_mode,
                status, scheduled_at, max_retries, idempotency_key,
                razorpay_subscription_id, razorpay_payment_id, amount_rupees,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id, customer_id, attempt_number, execution_mode,
                JOB_STATUS_SCHEDULED, scheduled_at, max_retries, idem_key,
                razorpay_subscription_id, razorpay_payment_id, amount_rupees,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        return True
    except sqlite3.IntegrityError:
        # UNIQUE violation on idempotency_key — already scheduled.
        return False


def claim_next_due_job(conn, now_iso: str = None) -> dict | None:
    """Atomically claim the next due, unclaimed job using BEGIN IMMEDIATE.

    Returns the claimed job row as a dict, or None if no due jobs exist.
    The job's status is updated to 'claimed' and claimed_at is set.
    This is the concurrency-safe worker pickup: two workers racing here will
    serialize at SQLite's write lock; only one sees the UPDATE take effect.
    """
    if now_iso is None:
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        conn.execute("BEGIN IMMEDIATE")
    except Exception:
        pass  # already in a transaction

    row = conn.execute(
        """
        SELECT * FROM recovery_jobs
        WHERE status = ? AND scheduled_at <= ?
        ORDER BY scheduled_at ASC, attempt_number ASC
        LIMIT 1
        """,
        (JOB_STATUS_SCHEDULED, now_iso),
    ).fetchone()

    if row is None:
        return None

    job = dict(row)
    conn.execute(
        "UPDATE recovery_jobs SET status = ?, claimed_at = ? WHERE job_id = ?",
        (JOB_STATUS_CLAIMED, now_iso, job["job_id"]),
    )
    job["status"] = JOB_STATUS_CLAIMED
    job["claimed_at"] = now_iso
    return job


def update_job_result(conn, job_id: str, status: str,
                      outcome: str = None,
                      executed_at: str = None,
                      failure_reason: str = None,
                      razorpay_payment_id: str = None,
                      razorpay_payment_link_id: str = None,
                      payment_link_url: str = None,
                      razorpay_subscription_id: str = None) -> None:
    """Persist the execution result back onto the job row."""
    if executed_at is None:
        executed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE recovery_jobs SET
            status = ?, outcome = ?, executed_at = ?,
            failure_reason = ?,
            razorpay_payment_id = ?,
            razorpay_payment_link_id = ?,
            payment_link_url = ?,
            razorpay_subscription_id = ?
        WHERE job_id = ?
        """,
        (
            status, outcome, executed_at, failure_reason,
            razorpay_payment_id, razorpay_payment_link_id,
            payment_link_url, razorpay_subscription_id,
            job_id,
        ),
    )


def increment_job_retry(conn, job_id: str) -> int:
    """Bump retry_count by 1. Returns the new count."""
    conn.execute(
        "UPDATE recovery_jobs SET retry_count = retry_count + 1, status = ? "
        "WHERE job_id = ?",
        (JOB_STATUS_SCHEDULED, job_id),
    )
    row = conn.execute(
        "SELECT retry_count FROM recovery_jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    return row["retry_count"] if row else 0


def cancel_job(conn, job_id: str, reason: str = None) -> None:
    """Mark a job cancelled (terminal; worker will not retry)."""
    conn.execute(
        "UPDATE recovery_jobs SET status = ?, failure_reason = ? WHERE job_id = ?",
        (JOB_STATUS_CANCELLED, reason, job_id),
    )


def get_job(conn, job_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM recovery_jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    return dict(row) if row else None


def get_jobs_for_case(conn, customer_id: str) -> list:
    """Return all recovery jobs for a customer, newest first."""
    rows = conn.execute(
        "SELECT * FROM recovery_jobs WHERE customer_id = ? ORDER BY attempt_number DESC",
        (customer_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_pending_jobs(conn, limit: int = 100) -> list:
    """Return scheduled/claimed jobs due now, ordered by scheduled_at ASC."""
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT * FROM recovery_jobs
        WHERE status IN (?, ?) AND scheduled_at <= ?
        ORDER BY scheduled_at ASC LIMIT ?
        """,
        (JOB_STATUS_SCHEDULED, JOB_STATUS_CLAIMED, now_iso, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_jobs(conn, limit: int = 500) -> list:
    """Return all recovery jobs, newest first (for dashboard/API)."""
    rows = conn.execute(
        "SELECT * FROM recovery_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def job_exists_for_attempt(conn, customer_id: str, attempt_number: int) -> bool:
    """Return True if a job already exists for this customer + attempt (idempotency check)."""
    idem_key = f"{customer_id}:{attempt_number}"
    row = conn.execute(
        "SELECT 1 FROM recovery_jobs WHERE idempotency_key = ?", (idem_key,)
    ).fetchone()
    return row is not None


# We need datetime/timezone for the job helpers above — import here to avoid
# polluting the top of the file (db.py intentionally has minimal imports).
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Phase 6: Strategy performance helpers
# ---------------------------------------------------------------------------

def upsert_strategy_performance(conn, strategy: str, dimension_key: str,
                                  dimension_value: str, provenance: str,
                                  delta_attempts: int = 0,
                                  delta_recoveries: int = 0,
                                  delta_amount_recovered: float = 0.0,
                                  delta_amount_attempted: float = 0.0,
                                  delta_escalations: int = 0,
                                  delta_time_hours: float = 0.0) -> None:
    """Atomically increment strategy_performance counters for one bucket."""
    conn.execute(
        """
        INSERT INTO strategy_performance
            (strategy, dimension_key, dimension_value, provenance,
             attempts, recoveries, amount_recovered, amount_attempted,
             escalations, time_to_recovery_sum_hours, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','utc'))
        ON CONFLICT(strategy, dimension_key, dimension_value, provenance) DO UPDATE SET
            attempts               = attempts + excluded.attempts,
            recoveries             = recoveries + excluded.recoveries,
            amount_recovered       = amount_recovered + excluded.amount_recovered,
            amount_attempted       = amount_attempted + excluded.amount_attempted,
            escalations            = escalations + excluded.escalations,
            time_to_recovery_sum_hours = time_to_recovery_sum_hours
                                    + excluded.time_to_recovery_sum_hours,
            last_updated           = datetime('now','utc')
        """,
        (strategy, dimension_key, dimension_value, provenance,
         delta_attempts, delta_recoveries,
         delta_amount_recovered, delta_amount_attempted,
         delta_escalations, delta_time_hours),
    )


def get_strategy_performance(conn, strategy: str = None,
                              dimension_key: str = None,
                              dimension_value: str = None,
                              provenance: str = None) -> list:
    """Query strategy_performance with optional filters. Returns list of dicts."""
    clauses, params = [], []
    if strategy:
        clauses.append("strategy = ?"); params.append(strategy)
    if dimension_key:
        clauses.append("dimension_key = ?"); params.append(dimension_key)
    if dimension_value:
        clauses.append("dimension_value = ?"); params.append(dimension_value)
    if provenance:
        clauses.append("provenance = ?"); params.append(provenance)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM strategy_performance {where} ORDER BY strategy, dimension_key, dimension_value",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Phase 6: Experiment helpers
# ---------------------------------------------------------------------------

def create_experiment(conn, experiment_id: str, name: str, description: str,
                      control_strategy: str, treatment_strategy: str,
                      cohort_definition: str,
                      merchant_category: str = None,
                      failure_reason: str = None,
                      min_sample_size: int = 30,
                      created_by: str = "system") -> bool:
    """Insert a new experiment row. Returns True on success."""
    try:
        conn.execute(
            """
            INSERT INTO experiments (
                experiment_id, name, description, merchant_category, failure_reason,
                control_strategy, treatment_strategy, cohort_definition,
                status, min_sample_size, created_by, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?,
                      datetime('now','utc'))
            """,
            (experiment_id, name, description, merchant_category, failure_reason,
             control_strategy, treatment_strategy, cohort_definition,
             min_sample_size, created_by),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def get_experiment(conn, experiment_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM experiments WHERE experiment_id = ?", (experiment_id,)
    ).fetchone()
    return dict(row) if row else None


def get_all_experiments(conn, status: str = None) -> list:
    if status:
        rows = conn.execute(
            "SELECT * FROM experiments WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM experiments ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def assign_experiment_case(conn, experiment_id: str, customer_id: str,
                            arm: str) -> bool:
    """Assign a case to a control/treatment arm. Returns False if already assigned."""
    try:
        conn.execute(
            """
            INSERT INTO experiment_assignments (experiment_id, customer_id, arm)
            VALUES (?, ?, ?)
            """,
            (experiment_id, customer_id, arm),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def get_experiment_assignments(conn, experiment_id: str) -> list:
    rows = conn.execute(
        "SELECT * FROM experiment_assignments WHERE experiment_id = ? ORDER BY id",
        (experiment_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_case_experiment_arm(conn, customer_id: str) -> dict | None:
    """Return the active experiment assignment for a case, or None."""
    row = conn.execute(
        """
        SELECT ea.*, e.control_strategy, e.treatment_strategy, e.status as exp_status
        FROM experiment_assignments ea
        JOIN experiments e ON ea.experiment_id = e.experiment_id
        WHERE ea.customer_id = ? AND e.status = 'active'
        ORDER BY ea.id DESC LIMIT 1
        """,
        (customer_id,),
    ).fetchone()
    return dict(row) if row else None


def record_experiment_outcome(conn, experiment_id: str, customer_id: str,
                               arm: str, strategy_used: str,
                               outcome_status: str, amount_rupees: float,
                               recovered: int,
                               time_to_recovery_hours: float = None,
                               execution_mode: str = "simulation") -> bool:
    """Record the final outcome for a case in an experiment (once, immutable)."""
    try:
        conn.execute(
            """
            INSERT INTO experiment_outcomes (
                experiment_id, customer_id, arm, strategy_used,
                outcome_status, amount_rupees, recovered,
                time_to_recovery_hours, execution_mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (experiment_id, customer_id, arm, strategy_used,
             outcome_status, amount_rupees, recovered,
             time_to_recovery_hours, execution_mode),
        )
        return True
    except sqlite3.IntegrityError:
        return False  # already recorded; immutable


def get_experiment_outcomes(conn, experiment_id: str) -> list:
    rows = conn.execute(
        "SELECT * FROM experiment_outcomes WHERE experiment_id = ? ORDER BY id",
        (experiment_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_experiment_status(conn, experiment_id: str, status: str,
                              ended_at: str = None) -> None:
    conn.execute(
        "UPDATE experiments SET status = ?, ended_at = ? WHERE experiment_id = ?",
        (status, ended_at, experiment_id),
    )


# ---------------------------------------------------------------------------
# Phase 6: Policy version helpers
# ---------------------------------------------------------------------------

def get_next_version_number(conn, merchant_category: str = "all") -> int:
    """Return the next sequential version number for a merchant category."""
    row = conn.execute(
        "SELECT MAX(version_number) FROM policy_versions WHERE merchant_category = ?",
        (merchant_category,),
    ).fetchone()
    current = row[0] if row and row[0] is not None else 0
    return current + 1


def create_policy_version(conn, version_id: str, merchant_category: str,
                           strategy_params: str, reason: str,
                           evidence_summary: str = None,
                           created_by: str = "system",
                           previous_version_id: str = None,
                           expected_impact: str = None) -> dict:
    """Create a new policy version in DRAFT status."""
    version_number = get_next_version_number(conn, merchant_category)
    conn.execute(
        """
        INSERT INTO policy_versions (
            version_id, version_number, merchant_category,
            strategy_params, status, created_by, reason,
            evidence_summary, previous_version_id, expected_impact
        ) VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)
        """,
        (version_id, version_number, merchant_category,
         strategy_params, created_by, reason,
         evidence_summary, previous_version_id, expected_impact),
    )
    _append_policy_audit(conn, "version_created", version_id=version_id,
                         actor=created_by, new_status="draft",
                         notes=reason)
    return {"version_id": version_id, "version_number": version_number}


def transition_policy_version(conn, version_id: str, new_status: str,
                               actor: str = "system",
                               notes: str = None) -> bool:
    """Move a policy version to a new status. Validates legal transitions."""
    _LEGAL = {
        "draft": {"recommended", "deprecated"},
        "recommended": {"under_review", "deprecated"},
        "under_review": {"approved", "deprecated"},
        "approved": {"active", "deprecated"},
        "active": {"deprecated", "rolled_back"},
        "deprecated": set(),
        "rolled_back": set(),
    }
    row = conn.execute(
        "SELECT status FROM policy_versions WHERE version_id = ?", (version_id,)
    ).fetchone()
    if not row:
        return False
    current = row["status"]
    if new_status not in _LEGAL.get(current, set()):
        return False

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    updates = {"status": new_status}
    if new_status == "active":
        updates["activated_at"] = now
        updates["approved_by"] = actor
        updates["approved_at"] = now
        # Deprecate any other currently-active version for the same merchant
        _deprecate_other_active(conn, version_id, actor)
    elif new_status in ("deprecated", "rolled_back"):
        updates["deprecated_at"] = now

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE policy_versions SET {set_clause} WHERE version_id = ?",
        list(updates.values()) + [version_id],
    )
    _append_policy_audit(conn, f"version_{new_status}", version_id=version_id,
                         actor=actor, previous_status=current,
                         new_status=new_status, notes=notes)
    return True


def _deprecate_other_active(conn, except_version_id: str, actor: str) -> None:
    """Deprecate all currently-active versions except the given one (same merchant)."""
    row = conn.execute(
        "SELECT merchant_category FROM policy_versions WHERE version_id = ?",
        (except_version_id,),
    ).fetchone()
    if not row:
        return
    cat = row["merchant_category"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    others = conn.execute(
        """SELECT version_id FROM policy_versions
           WHERE status = 'active' AND merchant_category = ?
             AND version_id != ?""",
        (cat, except_version_id),
    ).fetchall()
    for r in others:
        conn.execute(
            "UPDATE policy_versions SET status = 'deprecated', deprecated_at = ? "
            "WHERE version_id = ?",
            (now, r["version_id"]),
        )
        _append_policy_audit(conn, "version_deprecated", version_id=r["version_id"],
                             actor=actor, previous_status="active",
                             new_status="deprecated",
                             notes="Auto-deprecated on new version activation")


def get_policy_version(conn, version_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM policy_versions WHERE version_id = ?", (version_id,)
    ).fetchone()
    return dict(row) if row else None


def get_active_policy_version(conn, merchant_category: str = "all") -> dict | None:
    row = conn.execute(
        """SELECT * FROM policy_versions
           WHERE status = 'active' AND merchant_category = ?
           ORDER BY version_number DESC LIMIT 1""",
        (merchant_category,),
    ).fetchone()
    return dict(row) if row else None


def get_all_policy_versions(conn, merchant_category: str = None,
                             status: str = None) -> list:
    clauses, params = [], []
    if merchant_category:
        clauses.append("merchant_category = ?"); params.append(merchant_category)
    if status:
        clauses.append("status = ?"); params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM policy_versions {where} ORDER BY version_number DESC",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def record_policy_performance(conn, version_id: str, cases_observed: int,
                               recoveries: int, recovery_rate: float,
                               amount_recovered: float, escalation_rate: float,
                               measurement_window_days: int = 30,
                               data_type: str = "actual") -> None:
    conn.execute(
        """
        INSERT INTO policy_performance (
            version_id, measurement_window_days, cases_observed,
            recoveries, recovery_rate, amount_recovered,
            escalation_rate, data_type
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (version_id, measurement_window_days, cases_observed,
         recoveries, recovery_rate, amount_recovered,
         escalation_rate, data_type),
    )


def get_policy_performance(conn, version_id: str) -> list:
    rows = conn.execute(
        "SELECT * FROM policy_performance WHERE version_id = ? ORDER BY measured_at DESC",
        (version_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Phase 6: Policy recommendation helpers
# ---------------------------------------------------------------------------

def create_policy_recommendation(conn, recommendation_id: str, title: str,
                                   what_changes: str, why_evidence: str,
                                   current_strategy: str,
                                   recommended_strategy: str,
                                   current_rate: float = None,
                                   recommended_rate: float = None,
                                   sample_size: int = 0,
                                   estimated_incremental_rs: float = None,
                                   confidence: str = "low",
                                   data_source: str = "SIMULATION",
                                   merchant_category: str = "all",
                                   failure_reason: str = None,
                                   experiment_id: str = None) -> bool:
    try:
        conn.execute(
            """
            INSERT INTO policy_recommendations (
                recommendation_id, title, what_changes, why_evidence,
                current_strategy, recommended_strategy,
                current_rate, recommended_rate, sample_size,
                estimated_incremental_rs, confidence, data_source,
                status, merchant_category, failure_reason, experiment_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'recommended', ?, ?, ?)
            """,
            (recommendation_id, title, what_changes, why_evidence,
             current_strategy, recommended_strategy,
             current_rate, recommended_rate, sample_size,
             estimated_incremental_rs, confidence, data_source,
             merchant_category, failure_reason, experiment_id),
        )
        _append_policy_audit(conn, "recommendation_created",
                             recommendation_id=recommendation_id,
                             new_status="recommended")
        return True
    except sqlite3.IntegrityError:
        return False


def get_recommendation(conn, recommendation_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM policy_recommendations WHERE recommendation_id = ?",
        (recommendation_id,),
    ).fetchone()
    return dict(row) if row else None


def get_all_recommendations(conn, status: str = None,
                             merchant_category: str = None) -> list:
    clauses, params = [], []
    if status:
        clauses.append("status = ?"); params.append(status)
    if merchant_category:
        clauses.append("merchant_category = ?"); params.append(merchant_category)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM policy_recommendations {where} ORDER BY created_at DESC",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def update_recommendation_status(conn, recommendation_id: str, new_status: str,
                                   actor: str = "system",
                                   rejection_reason: str = None,
                                   policy_version_id: str = None) -> bool:
    _LEGAL = {
        "draft": {"recommended", "rejected"},
        "recommended": {"under_review", "approved", "rejected", "superseded"},
        "under_review": {"approved", "rejected"},
        "approved": {"superseded"},
        "rejected": set(),
        "superseded": set(),
    }
    row = conn.execute(
        "SELECT status FROM policy_recommendations WHERE recommendation_id = ?",
        (recommendation_id,),
    ).fetchone()
    if not row:
        return False
    current = row["status"]
    if new_status not in _LEGAL.get(current, set()):
        return False

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    updates: dict = {"status": new_status}
    if new_status == "approved":
        updates["approved_at"] = now
        updates["approved_by"] = actor
        if policy_version_id:
            updates["policy_version_id"] = policy_version_id
    elif new_status == "rejected":
        updates["rejected_at"] = now
        updates["rejected_by"] = actor
        if rejection_reason:
            updates["rejection_reason"] = rejection_reason
    elif new_status in ("under_review",):
        updates["reviewed_at"] = now
        updates["reviewed_by"] = actor

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE policy_recommendations SET {set_clause} WHERE recommendation_id = ?",
        list(updates.values()) + [recommendation_id],
    )
    _append_policy_audit(conn, f"recommendation_{new_status}",
                         recommendation_id=recommendation_id,
                         actor=actor, previous_status=current,
                         new_status=new_status, notes=rejection_reason)
    return True


# ---------------------------------------------------------------------------
# Phase 6: Policy audit log
# ---------------------------------------------------------------------------

def _append_policy_audit(conn, action_type: str,
                          version_id: str = None,
                          recommendation_id: str = None,
                          actor: str = "system",
                          previous_status: str = None,
                          new_status: str = None,
                          notes: str = None) -> None:
    conn.execute(
        """
        INSERT INTO policy_audit_log
            (action_type, version_id, recommendation_id, actor,
             previous_status, new_status, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (action_type, version_id, recommendation_id, actor,
         previous_status, new_status, notes),
    )


def get_policy_audit_log(conn, version_id: str = None,
                          recommendation_id: str = None,
                          limit: int = 200) -> list:
    clauses, params = [], []
    if version_id:
        clauses.append("version_id = ?"); params.append(version_id)
    if recommendation_id:
        clauses.append("recommendation_id = ?"); params.append(recommendation_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM policy_audit_log {where} ORDER BY action_at DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Phase 6: Phase 6 schema migration (run inside _migrate)
# ---------------------------------------------------------------------------

def _migrate_phase6(conn) -> None:
    """Apply Phase 6 table creation (idempotent via CREATE IF NOT EXISTS)."""
    # All Phase 6 tables are created inside _migrate() via executescript above.
    # This function handles additive column migrations for Phase 6 tables on
    # pre-existing databases that already have the tables but are missing new columns.
    existing_pv = {row[1] for row in conn.execute("PRAGMA table_info(policy_versions)")}
    additive_pv = {
        "expected_impact": "TEXT",
        "previous_version_id": "TEXT",
    }
    for col, decl in additive_pv.items():
        if col not in existing_pv:
            try:
                conn.execute(f"ALTER TABLE policy_versions ADD COLUMN {col} {decl}")
            except Exception:
                pass

    existing_pr = {row[1] for row in conn.execute("PRAGMA table_info(policy_recommendations)")}
    additive_pr = {
        "failure_reason": "TEXT",
        "experiment_id": "TEXT",
        "policy_version_id": "TEXT",
    }
    for col, decl in additive_pr.items():
        if col not in existing_pr:
            try:
                conn.execute(f"ALTER TABLE policy_recommendations ADD COLUMN {col} {decl}")
            except Exception:
                pass
