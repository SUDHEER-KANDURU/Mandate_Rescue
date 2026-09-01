"""Pytest root conftest: make backend/ importable as top-level modules.

Every backend module uses bare imports (e.g. `import db`, `import agent`) rather
than a package-relative style, matching how app.py runs them as the Flask
entrypoint's siblings. Tests replicate that by adding backend/ to sys.path once,
here, rather than restructuring the existing import style project-wide.
"""

import os
import sys

_BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

# webhook_security.py / razorpay_adapter.py are fail-closed: signing/verifying
# raises or returns False without a real secret configured. Tests need a stable,
# valid (non-placeholder) secret regardless of the developer's local .env, so set
# one here if the environment doesn't already provide one. This is a fixed
# TEST-ONLY value — never used for anything beyond this test process.
os.environ.setdefault("WEBHOOK_SECRET", "test-only-webhook-secret-not-for-real-use-0123456789abcdef")
os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "test-only-razorpay-secret-not-for-real-use-0123456789abcdef")
os.environ.setdefault("MANDATE_RESCUE_API_KEY", "test-only-api-key-0123456789abcdef")

