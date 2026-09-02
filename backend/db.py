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
