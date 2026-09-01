"""Internal API-key gate for mutating endpoints (security hardening, additive).

Mandate Rescue is a local single-user demo, but it exposes several endpoints that
mutate state (`/api/reset`, `/api/seed`, `/api/run-agent`, `/api/simulate`). With no
protection at all, anyone who can reach the Flask process (e.g. if it were ever
exposed on a shared network) could wipe or reseed the database or spin up simulation
load with a single unauthenticated POST. This module adds a minimal, dependency-free
API-key check for exactly those routes.

Design:
- The key is read from env `MANDATE_RESCUE_API_KEY`. If unset, a random key is
  generated ONCE at process startup and printed to the server log — the app still
  works out of the box for local single-user use (the frontend picks the key up from
  a same-origin `/api/_client-key` bootstrap call), but a value is always required to
  call a mutating endpoint. There is no "no key needed" mode.
- Clients send the key via the `X-API-Key` header. The dashboard's own JS attaches
  it automatically (fetched once at page load), so normal use of the UI is
  unaffected; only direct/external callers need to know the key.
- Comparison uses `hmac.compare_digest` (constant-time) to avoid timing side-channel
  leaks on the key check, same rationale as the webhook HMAC comparison.
- This is intentionally NOT a full auth system (no users, sessions, or roles) — it's
  a single shared secret, appropriate for a demo/hackathon deployment. A production
  deployment would replace this with real authentication.
"""

import hmac
import logging
import os
import secrets

log = logging.getLogger("mandate_rescue.security")

_ENV_VAR = "MANDATE_RESCUE_API_KEY"

# Generated once per process if the env var isn't set, so the app still runs
# out-of-the-box for a local demo without a hardcoded, publicly-known key.
_generated_key = None


def get_api_key():
    """Return the effective API key: the configured env value, or a process-local
    randomly generated one (generated once, logged at first use)."""
    global _generated_key
    configured = os.environ.get(_ENV_VAR)
    if configured:
        return configured
    if _generated_key is None:
        _generated_key = secrets.token_hex(24)
        log.warning(
            "%s is not set; generated a random per-process API key for this run. "
            "Set %s in your environment/.env for a stable key across restarts. "
            "Mutating endpoints (/api/reset, /api/seed, /api/run-agent, "
            "/api/simulate) require this key via the X-API-Key header.",
            _ENV_VAR, _ENV_VAR,
        )
    return _generated_key


def is_valid_key(candidate):
    """Constant-time check that `candidate` matches the effective API key."""
    if not candidate or not isinstance(candidate, str):
        return False
    return hmac.compare_digest(get_api_key(), candidate)
