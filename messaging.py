"""Message generation (design.md section 6, R9).

Generates real nudge text per case, templated by failure_reason and merchant_category,
in two tones (Standard English + Hinglish) across simulated channels. Messages are
generated and logged only, never actually delivered.
"""

CHANNELS = ["SMS", "WhatsApp", "Email"]

# Human-readable label per merchant category, used in message bodies.
CATEGORY_LABEL = {
    "subscription": "subscription",
    "emi": "EMI",
    "insurance": "insurance premium",
    "utility": "utility bill",
}


def _mask(customer_id):
    """Mask a synthetic customer id for display (keeps last 4 chars)."""
    cid = str(customer_id)
    if len(cid) <= 4:
        return cid
    return "****" + cid[-4:]


# Templates keyed by failure_reason. {amt} = amount, {cat} = category label.
STANDARD_TEMPLATES = {
    "insufficient_funds": "Hi, your {cat} auto-pay of Rs {amt} did not go through due to low balance. Please keep funds ready and we will retry automatically. No action needed if balance is topped up.",
    "mandate_expired": "Hi, your {cat} auto-pay of Rs {amt} could not be collected because your UPI mandate has expired. Please re-authorize using the secure link we sent to resume payments.",
    "bank_technical_error": "Hi, your {cat} payment of Rs {amt} could not be processed due to a temporary bank issue. We are retrying shortly, no action needed from your side.",
    "mandate_revoked": "Hi, we noticed your {cat} auto-pay mandate for Rs {amt} was cancelled. If this was not intended, please set up auto-pay again or reach out to us.",
}

HINGLISH_TEMPLATES = {
    "insufficient_funds": "Hi! Aapka {cat} ka auto-pay Rs {amt} balance kam hone ki wajah se fail ho gaya. Thoda balance rakhiye, hum automatically dobara try karenge. Tension mat lijiye!",
    "mandate_expired": "Hi! Aapka {cat} auto-pay Rs {amt} nahi ho paaya kyunki UPI mandate expire ho gaya hai. Neeche diye secure link se re-authorize kar dijiye, bas ek minute ka kaam hai.",
    "bank_technical_error": "Hi! {cat} ka Rs {amt} payment bank ki choti si technical dikkat ki wajah se ruk gaya. Hum thodi der mein dobara try kar rahe hain, aapko kuch karne ki zarurat nahi.",
    "mandate_revoked": "Hi! Aapka {cat} auto-pay mandate (Rs {amt}) cancel ho gaya hai. Agar galti se hua hai to dobara set up kar dijiye ya humse baat kijiye.",
}


def generate_messages(case, channel="SMS"):
    """Return dict with standard + hinglish message text for a case."""
    reason = case.get("failure_reason", "")
    amt = int(round(float(case.get("amount", 0))))
    cat = CATEGORY_LABEL.get(case.get("merchant_category", ""), "payment")
    std = STANDARD_TEMPLATES.get(reason, "Hi, your payment of Rs {amt} needs attention.")
    hin = HINGLISH_TEMPLATES.get(reason, "Hi! Rs {amt} ka payment pending hai, dhyan dijiye.")
    if channel not in CHANNELS:
        channel = "SMS"
    return {
        "customer": _mask(case.get("customer_id")),
        "channel": channel,
        "channels_available": CHANNELS,
        "standard": std.format(amt=amt, cat=cat),
        "hinglish": hin.format(amt=amt, cat=cat),
    }
