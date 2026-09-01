"""Shared fixtures for the backend test suite.

`fresh_db` gives each test an isolated in-memory SQLite database seeded with the
standard 180 synthetic cases — the exact same helper pattern chaos_test.py already
uses for its adversarial scenarios, now available to every test via a fixture so
individual test functions stay short.
"""

import random

import pytest

import db
import seed as seed_module


@pytest.fixture
def fresh_db():
    """Isolated in-memory DB seeded with 180 cases. Closed automatically after the test."""
    conn = db.get_memory_connection()
    db.init_db(conn)
    rng = random.Random(seed_module.SEED)
    records = seed_module.build_records(rng)
    for rec in records:
        db.insert_mandate_failure(conn, rec)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def empty_db():
    """Isolated in-memory DB with schema only, no rows."""
    conn = db.get_memory_connection()
    db.init_db(conn)
    yield conn
    conn.close()
