"""Unit tests for razorpay_adapter.py: real Razorpay signature scheme + event mapping."""

import hashlib
import hmac
import json

import pytest

import razorpay_adapter as ra


def _sign(raw_body_bytes, secret):
    return hmac.new(secret.encode("utf-8"), raw_body_bytes, hashlib.sha256).hexdigest()


SAMPLE_EVENT = {
    "event": "payment.failed",
    "created_at": 1704067200,
    "payload": {
        "payment": {
            "entity": {
                "id": "pay_test123",
                "amount": 150000,  # paise -> Rs 1500.00
                "notes": {},
            }
        },
        "subscription": {
            "entity": {
                "id": "sub_test123",
                "notes": {
                    "customer_id": "CUST9999",
                    "merchant_category": "subscription",
                    "customer_tenure_months": "6",
                    "past_payment_success_rate": "0.75",
                    "mandate_limit": "5000",
                },
            }
        },
    },
}


def test_verify_razorpay_signature_roundtrip(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "a-real-test-secret-not-a-placeholder")
    raw = json.dumps(SAMPLE_EVENT).encode("utf-8")
    sig = _sign(raw, "a-real-test-secret-not-a-placeholder")
    assert ra.verify_razorpay_signature(raw, sig) is True


def test_verify_rejects_wrong_signature(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "a-real-test-secret-not-a-placeholder")
    raw = json.dumps(SAMPLE_EVENT).encode("utf-8")
    assert ra.verify_razorpay_signature(raw, "0" * 64) is False


def test_verify_rejects_missing_signature(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "a-real-test-secret-not-a-placeholder")
    raw = json.dumps(SAMPLE_EVENT).encode("utf-8")
    assert ra.verify_razorpay_signature(raw, None) is False
    assert ra.verify_razorpay_signature(raw, "") is False


def test_verify_fails_closed_on_placeholder_secret(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "changeme")
    raw = json.dumps(SAMPLE_EVENT).encode("utf-8")
    sig = _sign(raw, "changeme")  # even a "correct" sig under the placeholder is rejected
    assert ra.verify_razorpay_signature(raw, sig) is False


def test_verify_detects_body_tampering(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "a-real-test-secret-not-a-placeholder")
    raw = json.dumps(SAMPLE_EVENT).encode("utf-8")
    sig = _sign(raw, "a-real-test-secret-not-a-placeholder")
    tampered = raw.replace(b"150000", b"1")
    assert ra.verify_razorpay_signature(tampered, sig) is False


def test_map_razorpay_event_extracts_case_fields():
    record = ra.map_razorpay_event(SAMPLE_EVENT)
    assert record is not None
    assert record["customer_id"] == "CUST9999"
    assert record["amount"] == pytest.approx(1500.0)
    assert record["failure_reason"] == "insufficient_funds"
    assert record["raw_event_type"] == "payment.failed"
    assert record["merchant_category"] == "subscription"
    assert record["source"] == "razorpay_live"
    assert record["customer_tenure_months"] == 6
    assert record["past_payment_success_rate"] == pytest.approx(0.75)


def test_map_razorpay_event_unhandled_event_type_returns_none():
    event = {"event": "order.paid", "payload": {}}
    assert ra.map_razorpay_event(event) is None


def test_map_razorpay_event_falls_back_to_subscription_id_without_notes():
    event = {
        "event": "subscription.halted",
        "created_at": 1704067200,
        "payload": {
            "subscription": {"entity": {"id": "sub_noNotes", "notes": {}}},
        },
    }
    record = ra.map_razorpay_event(event)
    assert record is not None
    assert record["customer_id"] == "sub_noNotes"
    assert record["failure_reason"] == "mandate_revoked"
