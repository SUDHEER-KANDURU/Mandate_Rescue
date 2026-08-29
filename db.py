"""SQLite data-access layer for Mandate Rescue.

Thin, transparent wrapper over sqlite3 with parameterized SQL (no ORM), so every
row that backs a dashboard number is easy to audit. See design.md section 3.
"""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mandate_rescue.db")


def get_connection():
    """Return a SQLite connection with row access by column name and FK enforcement."""
    conn = sqlite3.connect(DB_PATH)
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
    history_success_days      TEXT
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
"""


def init_db(conn=None):
    """Create tables and indexes if they do not exist."""
    own = conn is None
    if own:
        conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        if own:
            conn.close()


def reset_db(conn=None):
    """Drop all rows so a fresh seed/run is reproducible. Keeps schema."""
    own = conn is None
    if own:
        conn = get_connection()
    try:
        init_db(conn)
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM mandate_failures")
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'audit_log'")
        conn.commit()
    finally:
        if own:
            conn.close()


# --- Query helpers ----------------------------------------------------------

MANDATE_COLUMNS = [
    "customer_id", "amount", "failure_reason", "failure_date", "past_retry_count",
    "customer_tenure_months", "past_payment_success_rate", "merchant_category",
    "case_status", "raw_event_type", "mandate_limit", "compliance_status",
    "dunning_stage", "health_score", "history_success_days",
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
