"""Demo/testing helper: send a correctly-signed, realistic Razorpay webhook to a
running Mandate Rescue instance (default http://127.0.0.1:5000).

This does NOT call the real Razorpay API — it constructs a payload shaped exactly
like a real Razorpay `payment.failed` / `subscription.*` webhook and signs it with
the same HMAC-SHA256-over-raw-body scheme Razorpay itself uses, so it exercises the
REAL verification path in backend/razorpay_adapter.py end-to-end. Useful for a demo
video: show this script sending a "real" webhook, then show the case appear on the
dashboard with the "Razorpay live" badge.

Usage:
    python scripts/send_test_razorpay_webhook.py [--url http://127.0.0.1:5000] \
        [--customer-id CUSTDEMO1] [--amount 2500] [--event payment.failed]

Requires RAZORPAY_WEBHOOK_SECRET to be set in the environment (or .env) to the SAME
value the running server is using, since this script signs the payload exactly as
Razorpay's dashboard-configured webhook secret would.
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

# Load .env from the project root if present, same convention as backend/app.py.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
except ImportError:
    pass


def build_payload(customer_id, amount_rupees, event, merchant_category):
    amount_paise = int(round(amount_rupees * 100))
    return {
        "event": event,
        "created_at": int(time.time()),
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_demo_" + customer_id.lower(),
                    "amount": amount_paise,
                    "notes": {},
                }
            },
            "subscription": {
                "entity": {
                    "id": "sub_demo_" + customer_id.lower(),
                    "notes": {
                        "customer_id": customer_id,
                        "merchant_category": merchant_category,
                        "customer_tenure_months": "8",
                        "past_payment_success_rate": "0.72",
                        "mandate_limit": "5000",
                    },
                }
            },
        },
    }


def sign(raw_body_bytes, secret):
    return hmac.new(secret.encode("utf-8"), raw_body_bytes, hashlib.sha256).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5000")
    parser.add_argument("--customer-id", default="CUSTDEMO1")
    parser.add_argument("--amount", type=float, default=2500.0)
    parser.add_argument("--event", default="payment.failed",
                        choices=["payment.failed", "subscription.charged.failed",
                                "subscription.halted", "subscription.cancelled",
                                "subscription.pending"])
    parser.add_argument("--merchant-category", default="subscription",
                        choices=["subscription", "emi", "insurance", "utility"])
    args = parser.parse_args()

    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET")
    if not secret or secret.strip().lower() in {"", "your_razorpay_dashboard_webhook_secret_here"}:
        print("RAZORPAY_WEBHOOK_SECRET is not set to a real value. Set it in .env "
              "to the SAME value your running server is using, then re-run.", file=sys.stderr)
        sys.exit(1)

    payload = build_payload(args.customer_id, args.amount, args.event, args.merchant_category)
    raw_body = json.dumps(payload).encode("utf-8")
    signature = sign(raw_body, secret)

    url = args.url.rstrip("/") + "/api/webhooks/razorpay"
    req = urllib.request.Request(
        url, data=raw_body, method="POST",
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature},
    )
    print(f"POST {url}")
    print(f"  event={args.event} customer_id={args.customer_id} amount=Rs{args.amount}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        print(f"  -> {resp.status} {json.dumps(body, indent=2)}")
    except urllib.error.HTTPError as e:
        print(f"  -> {e.code} {e.read().decode('utf-8', 'replace')}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"  -> Could not reach {url}: {e}. Is the server running?", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
