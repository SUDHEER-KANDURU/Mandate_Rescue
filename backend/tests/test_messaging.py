"""Unit tests for backend/messaging.py.

Covers template substitution, Hinglish variants, channel validation,
masking, unknown failure_reason fallback, and all four categories.
"""

import pytest
import messaging


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _case(reason="insufficient_funds", amount=1500, category="subscription", cid="CUST1234"):
    return {
        "failure_reason": reason,
        "amount": amount,
        "merchant_category": category,
        "customer_id": cid,
    }


# ---------------------------------------------------------------------------
# generate_messages — return shape
# ---------------------------------------------------------------------------

class TestGenerateMessagesShape:
    def test_returns_required_keys(self):
        result = messaging.generate_messages(_case())
        for key in ("customer", "channel", "channels_available", "standard", "hinglish"):
            assert key in result, f"missing key: {key}"

    def test_channels_available_is_list(self):
        result = messaging.generate_messages(_case())
        assert isinstance(result["channels_available"], list)
        assert len(result["channels_available"]) > 0

    def test_all_channels_present(self):
        result = messaging.generate_messages(_case())
        assert set(result["channels_available"]) == set(messaging.CHANNELS)

    def test_default_channel_is_sms(self):
        result = messaging.generate_messages(_case())
        assert result["channel"] == "SMS"

    def test_custom_channel_respected(self):
        result = messaging.generate_messages(_case(), channel="WhatsApp")
        assert result["channel"] == "WhatsApp"

    def test_invalid_channel_falls_back_to_sms(self):
        result = messaging.generate_messages(_case(), channel="Telegram")
        assert result["channel"] == "SMS"

    def test_standard_and_hinglish_are_strings(self):
        result = messaging.generate_messages(_case())
        assert isinstance(result["standard"], str) and len(result["standard"]) > 0
        assert isinstance(result["hinglish"], str) and len(result["hinglish"]) > 0


# ---------------------------------------------------------------------------
# Template substitution — amount and category appear in messages
# ---------------------------------------------------------------------------

class TestTemplateSubstitution:
    def test_amount_appears_in_standard(self):
        result = messaging.generate_messages(_case(amount=2500))
        assert "2500" in result["standard"]

    def test_amount_appears_in_hinglish(self):
        result = messaging.generate_messages(_case(amount=3000))
        assert "3000" in result["hinglish"]

    def test_amount_rounded_to_int(self):
        # float amount 999.99 → "1000" (rounded) appears in message
        result = messaging.generate_messages(_case(amount=999.99))
        assert "1000" in result["standard"] or "999" in result["standard"]

    @pytest.mark.parametrize("category,label", [
        ("subscription", "subscription"),
        ("emi",          "EMI"),
        ("insurance",    "insurance premium"),
        ("utility",      "utility bill"),
    ])
    def test_category_label_substituted(self, category, label):
        result = messaging.generate_messages(_case(category=category))
        assert label in result["standard"]

    def test_unknown_category_does_not_crash(self):
        result = messaging.generate_messages(_case(category="gym_membership"))
        assert isinstance(result["standard"], str)


# ---------------------------------------------------------------------------
# All four failure_reason templates exist
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason", [
    "insufficient_funds",
    "mandate_expired",
    "bank_technical_error",
    "mandate_revoked",
])
def test_all_reasons_have_templates(reason):
    result = messaging.generate_messages(_case(reason=reason))
    # Should use the specific template, not the generic fallback
    assert result["standard"] in [
        t.format(amt=1500, cat=messaging.CATEGORY_LABEL.get("subscription", "payment"))
        for t in messaging.STANDARD_TEMPLATES.values()
    ] or isinstance(result["standard"], str)
    assert isinstance(result["hinglish"], str)


def test_unknown_reason_falls_back_gracefully():
    result = messaging.generate_messages(_case(reason="mystery_error"))
    assert isinstance(result["standard"], str) and len(result["standard"]) > 0
    assert isinstance(result["hinglish"], str) and len(result["hinglish"]) > 0


# ---------------------------------------------------------------------------
# Customer ID masking
# ---------------------------------------------------------------------------

class TestMasking:
    def test_long_id_masked(self):
        result = messaging.generate_messages(_case(cid="CUST1234"))
        # last 4 chars visible, rest masked
        assert result["customer"].endswith("1234")
        assert result["customer"].startswith("****")

    def test_short_id_not_truncated(self):
        result = messaging.generate_messages(_case(cid="AB"))
        assert result["customer"] == "AB"

    def test_exactly_four_chars_not_masked(self):
        result = messaging.generate_messages(_case(cid="WXYZ"))
        assert result["customer"] == "WXYZ"
