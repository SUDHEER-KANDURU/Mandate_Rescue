"""Unit tests for the fail-closed webhook_security.py secret handling."""

import os

import pytest

import webhook_security as ws


SAMPLE_PAYLOAD = {
    "customer_id": "CUST9001",
    "raw_event_type": "payment.failed",
    "failure_date": "2026-01-01",
    "amount": 1500.0,
}


def test_sign_and_verify_roundtrip():
    sig = ws.sign_payload(SAMPLE_PAYLOAD)
    assert ws.verify_signature(SAMPLE_PAYLOAD, sig) is True


def test_verify_rejects_wrong_signature():
    assert ws.verify_signature(SAMPLE_PAYLOAD, "deadbeef" * 8) is False


def test_verify_rejects_missing_signature():
    assert ws.verify_signature(SAMPLE_PAYLOAD, None) is False
    assert ws.verify_signature(SAMPLE_PAYLOAD, "") is False


def test_verify_rejects_tampered_payload():
    sig = ws.sign_payload(SAMPLE_PAYLOAD)
    tampered = dict(SAMPLE_PAYLOAD, amount=999999.0)
    assert ws.verify_signature(tampered, sig) is False


@pytest.mark.parametrize("placeholder", [
    "", "your_secret_here", "demo-secret-change-me", "changeme", "SECRET",
])
def test_placeholder_secrets_fail_closed(monkeypatch, placeholder):
    monkeypatch.setenv("WEBHOOK_SECRET", placeholder)
    with pytest.raises(ws.WebhookSecretError):
        ws.sign_payload(SAMPLE_PAYLOAD)
    # verify_signature must fail closed (return False), never raise, even though
    # the secret is misconfigured.
    assert ws.verify_signature(SAMPLE_PAYLOAD, "anything") is False


def test_missing_secret_fails_closed(monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    with pytest.raises(ws.WebhookSecretError):
        ws.sign_payload(SAMPLE_PAYLOAD)
    assert ws.verify_signature(SAMPLE_PAYLOAD, "anything") is False
