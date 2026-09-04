
"""Multilingual Recovery Messaging - Phase 7.
Extends messaging.py with structured multilingual support.
Supports: English, Hinglish. Architecture allows future languages.
Never fabricates customer-specific details. No sensitive payment info in messages.
"""
import logging
log = logging.getLogger("mandate_rescue.multilingual")

SUPPORTED_LANGUAGES = ["en", "hi", "hinglish"]

# Recovery message templates per scenario + language
_TEMPLATES = {
    "failed_payment": {
        "en": "Your payment of Rs {amount} could not be processed. Please retry securely: {link}",
        "hinglish": "Aapka Rs {amount} ka payment process nahi ho paaya. Secure link se dobara try karein: {link}",
        "hi": "Aapka Rs {amount} ka bhugtaan nahi ho saka. Kripaya yahan se dobara try karein: {link}",
    },
    "failed_subscription": {
        "en": "Your subscription payment of Rs {amount} failed. Please update your payment method: {link}",
        "hinglish": "Aapka Rs {amount} subscription payment fail ho gaya. Payment method update karein: {link}",
        "hi": "Aapki Rs {amount} subscription ka bhugtaan fail ho gaya. Kripaya update karein: {link}",
    },
    "mandate_retry": {
        "en": "Your auto-pay of Rs {amount} could not be collected. We will retry automatically. No action needed if your balance is sufficient.",
        "hinglish": "Aapka Rs {amount} auto-pay collect nahi ho paaya. Hum automatically dobara try karenge. Balance theek hai to kuch karne ki zaroorat nahi.",
        "hi": "Aapka Rs {amount} auto-debit nahi ho saka. Hum dobara try karenge. Koi kadam uthane ki zaroorat nahi.",
    },
    "checkout_abandonment": {
        "en": "You left something behind! Complete your purchase of Rs {amount} here: {link}",
        "hinglish": "Aap kuch bhool gaye! Rs {amount} ki kharidaari poori karein: {link}",
        "hi": "Aapki kharidaari adhoori reh gayi. Rs {amount} ki purchase yahan poori karein: {link}",
    },
    "b2b_receivable": {
        "en": "Friendly reminder: Invoice {invoice_ref} for Rs {amount} is due. Please arrange payment at your earliest convenience.",
        "hinglish": "Yaad dilaate hain: Rs {amount} ka invoice {invoice_ref} due hai. Payment arrange karein.",
        "hi": "Vinrm: Invoice {invoice_ref} ke Rs {amount} baki hain. Kripaya jald bhugtaan karein.",
    },
    "promise_to_pay": {
        "en": "Just a reminder that your payment of Rs {amount} is due today as promised. Please complete the transfer.",
        "hinglish": "Reminder: Aapka Rs {amount} ka payment aaj due hai, jaise promise kiya tha. Transfer complete karein.",
        "hi": "Yaad dilaate hain: Aapka vaada tha Rs {amount} aaj dene ka. Transfer poora karein.",
    },
    "mandate_expired": {
        "en": "Your UPI mandate for Rs {amount} has expired. Please re-authorize using the link: {link}",
        "hinglish": "Aapka Rs {amount} ka UPI mandate expire ho gaya hai. Link se re-authorize karein: {link}",
        "hi": "Aapka Rs {amount} ka UPI mandate samapt ho gaya. Link se phir se authorize karein: {link}",
    },
    "insufficient_funds": {
        "en": "Your payment of Rs {amount} could not be processed due to insufficient funds. Please top up and we will retry.",
        "hinglish": "Rs {amount} payment insufficient balance ki wajah se fail hua. Balance top-up karein, hum retry karenge.",
        "hi": "Rs {amount} ka bhugtaan nahi hua - insufficient balance. Balance badhayein.",
    },
    "bank_technical_error": {
        "en": "Your payment of Rs {amount} faced a temporary bank issue. We are retrying automatically.",
        "hinglish": "Rs {amount} payment mein bank ki temporary dikkat aayi. Hum automatic retry kar rahe hain.",
        "hi": "Rs {amount} ke bhugtaan mein bank ki samasya aayi. Hum dobara try kar rahe hain.",
    },
    "escalation": {
        "en": "Your account requires attention. Rs {amount} remains unresolved. Please contact us to resolve.",
        "hinglish": "Aapke account mein Rs {amount} pending hai. Kripaya humse sampark karein.",
        "hi": "Aapke account mein Rs {amount} abhi bhi baaki hai. Sampark karein.",
    },
}
_DEFAULT_TEMPLATE = {
    "en": "Payment of Rs {amount} requires attention.",
    "hinglish": "Rs {amount} ka payment attention chahta hai.",
    "hi": "Rs {amount} ke bhugtaan par dhyan den.",
}


def generate_recovery_message(case, language="en", recovery_link=None,
                               invoice_ref=None):
    """Generate a recovery message for a case in the requested language.
    Returns dict with message text and metadata.
    Never includes sensitive payment credentials or internal IDs.
    """
    if language not in SUPPORTED_LANGUAGES:
        language = "en"
    scenario = case.get("scenario_type","failed_payment")
    reason   = case.get("failure_reason","")
    amount   = int(round(float(case.get("amount") or 0)))
    link     = recovery_link or "[secure payment link]"
    inv_ref  = invoice_ref or case.get("customer_ref","") or ""
    # Choose template key
    key = reason if reason in _TEMPLATES else scenario
    templates = _TEMPLATES.get(key, _DEFAULT_TEMPLATE)
    lang_key = language if language in templates else "en"
    template = templates.get(lang_key, templates.get("en",""))
    try:
        text = template.format(amount=f"{amount:,}", link=link, invoice_ref=inv_ref)
    except Exception:
        text = template
    return {
        "language": language,
        "scenario": scenario,
        "message": text,
        "channel_appropriate": True,
        "data_type": "SIMULATED",
        "note": "Message generated from template. Not actually delivered unless provider is configured.",
    }


def generate_all_languages(case, recovery_link=None, invoice_ref=None):
    """Generate messages in all supported languages for comparison."""
    return {lang: generate_recovery_message(case, language=lang,
                                            recovery_link=recovery_link,
                                            invoice_ref=invoice_ref)
            for lang in SUPPORTED_LANGUAGES}


def generate_voice_script(case, language="en", call_intent="recovery_reminder"):
    """Generate a voice call script. No actual call is made.
    Returns script text and metadata. Clearly labelled SIMULATED.
    """
    amount = int(round(float(case.get("amount") or 0)))
    scenario = case.get("scenario_type","failed_payment")
    SCRIPTS = {
        "en": {
            "recovery_reminder":
                f"Hello, this is an automated reminder from your payment provider. "
                f"A payment of Rupees {amount:,} is pending. "
                f"Please complete the payment at your earliest convenience. Thank you.",
            "promise_follow_up":
                f"Hello, this is a follow-up on your promised payment of Rupees {amount:,}. "
                f"Your payment is now due. Please complete the transfer today. Thank you.",
            "escalation":
                f"Hello, this is an important notice. Your outstanding balance of "
                f"Rupees {amount:,} requires immediate attention. "
                f"Please contact our team to resolve this.",
            "b2b_chase":
                f"Hello, this is a reminder about your outstanding invoice of "
                f"Rupees {amount:,}. Please arrange payment at your earliest convenience.",
        },
        "hinglish": {
            "recovery_reminder":
                f"Namaste, yeh ek automated reminder hai. "
                f"Rupees {amount:,} ka payment pending hai. "
                f"Kripaya jald se jald payment complete karein. Dhanyawad.",
            "promise_follow_up":
                f"Namaste, aapke promised payment Rupees {amount:,} ke baare mein follow-up. "
                f"Aaj payment due hai. Transfer complete karein. Dhanyawad.",
        },
    }
    lang_scripts = SCRIPTS.get(language, SCRIPTS["en"])
    script_text  = lang_scripts.get(call_intent, lang_scripts.get("recovery_reminder",""))
    return {
        "script_text": script_text,
        "language": language,
        "call_intent": call_intent,
        "scenario": scenario,
        "status": "READY_FOR_PROVIDER",
        "execution_mode": "SIMULATED",
        "note": (
            "Script ready for voice provider. No actual call made. "
            "Connect a voice provider adapter (Exotel, Twilio, etc.) to enable real calls."
        ),
        "data_type": "SIMULATED",
    }
