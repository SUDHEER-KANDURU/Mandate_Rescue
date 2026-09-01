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
    processed         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_webhook_events_event_id ON webhook_events(razorpay_event_id);
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
    add columns to an already-created table). Adds any missing whitelisted columns."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(mandate_failures)")}
    # column name -> SQL type/definition for additive ALTERs.
    additive = {
        "webhook_signature": "TEXT",
        "source": "TEXT NOT NULL DEFAULT 'synthetic'",
    }
    for col, decl in additive.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE mandate_failures ADD COLUMN {col} {decl}")


def reset_db(conn=None):
    """Drop all rows so a fresh seed/run is reproducible. Keeps schema."""
    own = conn is None
    if own:
        conn = get_connection()
    try:
        init_db(conn)
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM mandate_failures")
        conn.execute("DELETE FROM webhook_events")
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'audit_log'")
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'webhook_events'")
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
