"""Integration tests for the /api/ask endpoint (natural-language query).

The LLM is fully stubbed so tests run offline and deterministically. We exercise:
- successful query flow (LLM returns a valid filter spec)
- empty question guard
- LLM returns no usable filters (unclear question)
- LLM call fails (various error codes → correct user-facing messages)
- field whitelist enforcement (off-whitelist keys dropped)
- parameterized SQL safety (injection attempt in LLM output is blocked)
- actual result filtering (filter is applied and row set is correct)
"""

import json
import pytest

import db
import llm_client
import agent as agent_module


# ---------------------------------------------------------------------------
# Shared connection proxy (no-close) so Flask test client DB calls survive.
# ---------------------------------------------------------------------------

class _NoCloseProxy:
    def __init__(self, conn):
        object.__setattr__(self, '_conn', conn)

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_conn'), name)


# ---------------------------------------------------------------------------
# Flask test client fixture — seeds + runs agent so statuses are meaningful.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """Flask test client backed by a seeded + agent-run in-memory DB."""
    import random
    import seed as seed_module
    # Build an in-memory DB with a full agent run.
    conn = db.get_memory_connection()
    db.init_db(conn)
    rng = random.Random(seed_module.SEED)
    for rec in seed_module.build_records(rng):
        db.insert_mandate_failure(conn, rec)
    conn.commit()
    agent_module.run_agent(conn=conn)
    conn.commit()

    # Import app AFTER setting env so security.get_api_key() picks up the test key.
    import os
    os.environ.setdefault("MANDATE_RESCUE_API_KEY", "ci-test-api-key-0123456789abcdef")
    import app as flask_app
    flask_app.app.config["TESTING"] = True

    proxy = _NoCloseProxy(conn)
    with flask_app.app.test_client() as c:
        # Patch db.get_connection globally so every query inside the app uses our DB.
        import unittest.mock as mock
        with mock.patch.object(db, "get_connection", return_value=proxy):
            yield c

    conn.close()


# ---------------------------------------------------------------------------
# Helper: POST /api/ask
# ---------------------------------------------------------------------------

def _ask(client, question, extra=None):
    body = {"question": question}
    if extra:
        body.update(extra)
    return client.post(
        "/api/ask",
        data=json.dumps(body),
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# Empty question guard
# ---------------------------------------------------------------------------

def test_empty_question_returns_error(client):
    resp = _ask(client, "")
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is False
    assert data["reason"] == "empty"


def test_whitespace_only_question_returns_error(client):
    resp = _ask(client, "   ")
    data = resp.get_json()
    assert data["ok"] is False
    assert data["reason"] == "empty"


# ---------------------------------------------------------------------------
# LLM stubbed — translate_query returns a valid filter spec
# ---------------------------------------------------------------------------

def test_valid_filter_returns_results(client, monkeypatch):
    monkeypatch.setattr(
        llm_client, "translate_query",
        lambda q: {"failure_reason": "insufficient_funds"}
    )
    monkeypatch.setattr(
        llm_client, "summarize_results",
        lambda q, count, sample: f"Found {count} cases."
    )
    resp = _ask(client, "show insufficient_funds cases")
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["count"] > 0
    for row in data["results"]:
        assert row["failure_reason"] == "insufficient_funds"
    assert "filter" in data
    assert data["filter"].get("failure_reason") == "insufficient_funds"


def test_response_shape_on_success(client, monkeypatch):
    monkeypatch.setattr(llm_client, "translate_query",
                        lambda q: {"case_status": "recovered"})
    monkeypatch.setattr(llm_client, "summarize_results",
                        lambda q, count, sample: "summary")
    resp = _ask(client, "recovered cases")
    data = resp.get_json()
    assert data["ok"] is True
    for key in ("question", "filter", "count", "summary", "results"):
        assert key in data, f"missing key: {key}"


# ---------------------------------------------------------------------------
# LLM stubbed — translate_query returns empty dict (unclear question)
# ---------------------------------------------------------------------------

def test_unclear_question_returns_unclear_reason(client, monkeypatch):
    monkeypatch.setattr(llm_client, "translate_query", lambda q: {})
    resp = _ask(client, "blah blah xyz")
    data = resp.get_json()
    assert data["ok"] is False
    assert data["reason"] == "unclear"


# ---------------------------------------------------------------------------
# LLM call fails — various error codes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("err_code,expected_reason", [
    (llm_client.ERR_NO_KEY,      "unavailable"),
    (llm_client.ERR_RATE_LIMIT,  "rate_limited"),
    (llm_client.ERR_TIMEOUT,     "timeout"),
    (llm_client.ERR_NETWORK,     "unavailable"),
    (llm_client.ERR_HTTP,        "unavailable"),
])
def test_llm_error_returns_correct_reason(client, monkeypatch, err_code, expected_reason):
    monkeypatch.setattr(llm_client, "translate_query", lambda q: None)
    monkeypatch.setattr(llm_client, "last_error", lambda: err_code)
    resp = _ask(client, "show me something")
    data = resp.get_json()
    assert data["ok"] is False
    assert data["reason"] == expected_reason
    assert isinstance(data["message"], str) and len(data["message"]) > 0


# ---------------------------------------------------------------------------
# Field whitelist enforcement
# ---------------------------------------------------------------------------

def test_off_whitelist_keys_dropped(client, monkeypatch):
    """LLM output containing keys not on ASK_FIELD_WHITELIST must be silently dropped."""
    monkeypatch.setattr(
        llm_client, "translate_query",
        lambda q: {
            "failure_reason": "mandate_revoked",  # on whitelist
            "DROP TABLE": "mandate_failures",      # not on whitelist
            "evil_field": "'; DROP TABLE mandate_failures; --",
        }
    )
    monkeypatch.setattr(llm_client, "summarize_results",
                        lambda q, count, sample: "ok")
    resp = _ask(client, "anything")
    data = resp.get_json()
    # Should succeed — the valid key goes through, the bad ones are dropped.
    assert data["ok"] is True
    assert "DROP TABLE" not in data.get("filter", {})
    assert "evil_field" not in data.get("filter", {})
    # The valid filter was applied.
    assert data["filter"].get("failure_reason") == "mandate_revoked"


def test_all_off_whitelist_returns_unclear(client, monkeypatch):
    """If ALL LLM output keys are off-whitelist, the endpoint returns 'unclear'."""
    monkeypatch.setattr(
        llm_client, "translate_query",
        lambda q: {"completely_unknown_field": "some_value"}
    )
    resp = _ask(client, "whatever")
    data = resp.get_json()
    assert data["ok"] is False
    assert data["reason"] == "unclear"


# ---------------------------------------------------------------------------
# Result filtering correctness (without LLM — direct filter spec path)
# ---------------------------------------------------------------------------

def test_amount_min_filter_via_ask(client, monkeypatch):
    monkeypatch.setattr(llm_client, "translate_query",
                        lambda q: {"amount_min": 5000})
    monkeypatch.setattr(llm_client, "summarize_results",
                        lambda q, count, sample: "ok")
    resp = _ask(client, "cases above 5000")
    data = resp.get_json()
    assert data["ok"] is True
    for row in data["results"]:
        assert row["amount"] >= 5000


def test_recovered_status_filter(client, monkeypatch):
    monkeypatch.setattr(llm_client, "translate_query",
                        lambda q: {"case_status": "recovered"})
    monkeypatch.setattr(llm_client, "summarize_results",
                        lambda q, count, sample: "ok")
    resp = _ask(client, "recovered cases")
    data = resp.get_json()
    assert data["ok"] is True
    # After an agent run there should be recovered cases.
    assert data["count"] > 0
    for row in data["results"]:
        assert row["case_status"] == "recovered"


# ---------------------------------------------------------------------------
# No body / malformed body handled gracefully
# ---------------------------------------------------------------------------

def test_no_body_returns_empty_error(client):
    resp = client.post("/api/ask", content_type="application/json", data="")
    data = resp.get_json()
    assert data["ok"] is False
    assert data["reason"] == "empty"


def test_non_json_body_handled(client):
    resp = client.post("/api/ask", content_type="text/plain", data="hello")
    data = resp.get_json()
    assert data["ok"] is False
