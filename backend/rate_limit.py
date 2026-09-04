"""Simple in-process rate limiter for expensive endpoints (Phase 6.5).

Uses a sliding-window token-bucket per (endpoint, client_ip) so accidental or
malicious quota exhaustion is caught before it reaches the LLM or the DB.

No external dependencies — uses only the standard library.  Not suitable for
multi-process deployments (use Redis + lua scripts there); for a single-process
Flask demo this is correct and sufficient.

Configuration via environment variables:
    RATE_LIMIT_ASK_RPM       default 20   /api/ask requests per minute per IP
    RATE_LIMIT_INVESTIGATE_RPM  default 10
    RATE_LIMIT_AGENT_RPM     default 5    /api/run-agent* per minute per IP
    RATE_LIMIT_ENABLED       default 1    set to 0 to disable entirely
"""

import os
import threading
import time
from collections import deque
from typing import Dict, Tuple

_enabled = os.environ.get("RATE_LIMIT_ENABLED", "1").strip() != "0"

# Per-endpoint limits: (max_requests, window_seconds)
_LIMITS: Dict[str, Tuple[int, int]] = {
    "/api/ask":              (int(os.environ.get("RATE_LIMIT_ASK_RPM", "20")),        60),
    "/api/investigate":      (int(os.environ.get("RATE_LIMIT_INVESTIGATE_RPM", "10")), 60),
    "/api/run-agent":        (int(os.environ.get("RATE_LIMIT_AGENT_RPM", "5")),        60),
    "/api/run-agent-stream": (int(os.environ.get("RATE_LIMIT_AGENT_RPM", "5")),        60),
    "/api/run-agent-stream-token": (int(os.environ.get("RATE_LIMIT_AGENT_RPM", "5")), 60),
    "/api/simulate":         (int(os.environ.get("RATE_LIMIT_AGENT_RPM", "5")),        60),
    # Auth endpoints — stricter limits to prevent brute-force and email abuse
    "/api/auth/register":        (int(os.environ.get("RATE_LIMIT_REGISTER_RPM", "10")), 60),
    "/api/auth/login":           (int(os.environ.get("RATE_LIMIT_LOGIN_RPM", "10")),    60),
    "/api/auth/verify-email":    (int(os.environ.get("RATE_LIMIT_OTP_RPM", "6")),       60),
    "/api/auth/resend-otp":      (int(os.environ.get("RATE_LIMIT_OTP_RPM", "6")),       60),
    "/api/auth/forgot-password": (int(os.environ.get("RATE_LIMIT_OTP_RPM", "6")),       60),
    "/api/auth/reset-password":  (int(os.environ.get("RATE_LIMIT_OTP_RPM", "6")),       60),
    "/api/auth/change-password/request": (int(os.environ.get("RATE_LIMIT_OTP_RPM", "6")), 60),
    "/api/auth/change-password/confirm": (int(os.environ.get("RATE_LIMIT_OTP_RPM", "6")), 60),
    "/api/auth/change-email/request":    (int(os.environ.get("RATE_LIMIT_OTP_RPM", "6")), 60),
    "/api/auth/change-email/confirm":    (int(os.environ.get("RATE_LIMIT_OTP_RPM", "6")), 60),
}

# Sliding-window counters: (endpoint, client_key) -> deque of timestamps
_counters: Dict[Tuple[str, str], deque] = {}
_lock = threading.Lock()


def check(endpoint: str, client_key: str) -> Tuple[bool, dict]:
    """Check whether the client is within the rate limit for this endpoint.

    Returns (allowed: bool, info: dict).
    `info` contains limit, remaining, reset_after_seconds.

    If rate limiting is disabled or the endpoint has no configured limit,
    always returns (True, {}).
    """
    if not _enabled:
        return True, {}

    limit_cfg = _LIMITS.get(endpoint)
    if limit_cfg is None:
        return True, {}

    max_req, window_s = limit_cfg
    now = time.monotonic()

    with _lock:
        key = (endpoint, client_key)
        if key not in _counters:
            _counters[key] = deque()
        q = _counters[key]

        # Evict timestamps outside the window.
        cutoff = now - window_s
        while q and q[0] < cutoff:
            q.popleft()

        count = len(q)
        if count >= max_req:
            oldest = q[0]
            reset_after = int(window_s - (now - oldest)) + 1
            return False, {
                "limit":             max_req,
                "window_seconds":    window_s,
                "remaining":         0,
                "reset_after_seconds": reset_after,
            }

        # Consume one slot.
        q.append(now)
        return True, {
            "limit":          max_req,
            "window_seconds": window_s,
            "remaining":      max_req - count - 1,
        }


def flask_check(endpoint: str, request) -> Tuple[bool, dict]:
    """Convenience wrapper that extracts the client key from a Flask request."""
    # Use X-Forwarded-For if behind a proxy; fall back to remote_addr.
    xff = request.headers.get("X-Forwarded-For", "")
    client_ip = xff.split(",")[0].strip() if xff else (request.remote_addr or "unknown")
    return check(endpoint, client_ip)
