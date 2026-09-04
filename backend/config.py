"""Central configuration for Mandate Rescue (Phase 6.5).

All environment-variable reads and validation are consolidated here.
Modules import from config rather than calling os.environ directly, so:
  - Every config key is in one place
  - Startup validation catches missing/invalid values early
  - Tests can patch a single module instead of many os.environ calls

Environment variable names are preserved for backwards compatibility.
"""

import os
import logging

log = logging.getLogger("mandate_rescue.config")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    os.path.join(_PROJECT_ROOT, "mandate_rescue.db"),
)

# ---------------------------------------------------------------------------
# Razorpay
# ---------------------------------------------------------------------------
RAZORPAY_KEY_ID: str     = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET: str = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET: str = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

RAZORPAY_CONFIGURED: bool = bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET
                                  and not RAZORPAY_KEY_ID.startswith("rzp_placeholder"))

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
LLM_API_BASE: str    = os.environ.get("LLM_API_BASE", "https://api.groq.com/openai/v1")
LLM_MODEL: str       = os.environ.get("LLM_MODEL", "llama3-8b-8192")
LLM_TIMEOUT: float   = float(os.environ.get("LLM_TIMEOUT", "5"))
LLM_MAX_ATTEMPTS: int = int(os.environ.get("LLM_MAX_ATTEMPTS", "2"))
LLM_BACKOFF_BASE: float = float(os.environ.get("LLM_BACKOFF_BASE", "0.5"))
LLM_LIVE_TOP_N: int  = int(os.environ.get("LLM_LIVE_TOP_N", "5"))

GROQ_API_KEY: str    = os.environ.get("GROQ_API_KEY", "")
OPENAI_API_KEY: str  = os.environ.get("OPENAI_API_KEY", "")
LLM_API_KEY: str     = GROQ_API_KEY or OPENAI_API_KEY

LLM_CONFIGURED: bool = bool(LLM_API_KEY)

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
MANDATE_RESCUE_API_KEY: str = os.environ.get("MANDATE_RESCUE_API_KEY", "")
WEBHOOK_SECRET: str = os.environ.get("WEBHOOK_SECRET", "")   # synthetic webhook HMAC

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
SCHEDULER_POLL_S: float = float(os.environ.get("SCHEDULER_POLL_S", "30"))
SCHEDULER_STALE_MIN: int = int(os.environ.get("SCHEDULER_STALE_MIN", "15"))

# ---------------------------------------------------------------------------
# Execution mode
# ---------------------------------------------------------------------------
EXECUTION_MODE: str = os.environ.get("EXECUTION_MODE", "simulation")  # simulation | real_test

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()

# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
NOTIFICATION_PROVIDER: str = os.environ.get("NOTIFICATION_PROVIDER", "demo")

# ---------------------------------------------------------------------------
# Email / SMTP
# ---------------------------------------------------------------------------
SMTP_HOST: str              = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT: int              = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME: str          = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD: str          = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM: str              = os.environ.get("MAIL_FROM", SMTP_USERNAME)
MAIL_FROM_NAME: str         = os.environ.get("MAIL_FROM_NAME", "Mandate Rescue")
NOTIFICATION_EMAIL_PROVIDER: str = os.environ.get("NOTIFICATION_EMAIL_PROVIDER", "simulated")
# "google_smtp" | "simulated"

EMAIL_CONFIGURED: bool      = bool(SMTP_USERNAME and SMTP_PASSWORD)

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
RATE_LIMIT_ENABLED: bool = os.environ.get("RATE_LIMIT_ENABLED", "1").strip() != "0"
RATE_LIMIT_ASK_RPM: int = int(os.environ.get("RATE_LIMIT_ASK_RPM", "20"))
RATE_LIMIT_INVESTIGATE_RPM: int = int(os.environ.get("RATE_LIMIT_INVESTIGATE_RPM", "10"))
RATE_LIMIT_AGENT_RPM: int = int(os.environ.get("RATE_LIMIT_AGENT_RPM", "5"))

# Auth rate limits
RATE_LIMIT_REGISTER_RPM: int  = int(os.environ.get("RATE_LIMIT_REGISTER_RPM", "10"))
RATE_LIMIT_LOGIN_RPM: int     = int(os.environ.get("RATE_LIMIT_LOGIN_RPM", "10"))
RATE_LIMIT_OTP_RPM: int       = int(os.environ.get("RATE_LIMIT_OTP_RPM", "6"))


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

def validate(strict: bool = False) -> list:
    """Validate critical configuration.  Returns a list of warning/error strings.

    If `strict=True`, raises ValueError on the first critical failure (for use
    in production startup scripts where a misconfigured app must not start).
    Otherwise returns the list so the caller can log/display and continue.
    """
    issues = []

    # LLM key — not critical (graceful degradation to templates)
    if not LLM_CONFIGURED:
        issues.append("WARNING: No LLM API key configured (GROQ_API_KEY / OPENAI_API_KEY). "
                       "All LLM features will use template fallbacks.")

    # Razorpay — not critical for demo/simulation
    if not RAZORPAY_CONFIGURED:
        issues.append("INFO: Razorpay credentials not configured. "
                       "Running in simulation mode only.")

    # Webhook secret — only warn if Razorpay is configured
    if RAZORPAY_CONFIGURED and not RAZORPAY_WEBHOOK_SECRET:
        msg = "WARNING: RAZORPAY_WEBHOOK_SECRET is not set. Webhook signature verification disabled."
        issues.append(msg)
        if strict:
            raise ValueError(msg)

    # Synthetic webhook secret
    if not WEBHOOK_SECRET:
        issues.append("INFO: WEBHOOK_SECRET not set; synthetic webhook signatures will be empty.")

    return issues


def log_startup_config() -> None:
    """Log a safe summary of the current configuration at startup."""
    lines = [
        f"Database:  {DATABASE_URL}",
        f"LLM:       {'configured (' + LLM_MODEL + ')' if LLM_CONFIGURED else 'not configured (template fallbacks)'}",
        f"Razorpay:  {'configured' if RAZORPAY_CONFIGURED else 'not configured (simulation only)'}",
        f"Email:     {NOTIFICATION_EMAIL_PROVIDER} {'(SMTP configured)' if EMAIL_CONFIGURED else '(no SMTP credentials — simulated)'}",
        f"Scheduler: poll={SCHEDULER_POLL_S}s stale_window={SCHEDULER_STALE_MIN}min",
        f"Exec mode: {EXECUTION_MODE}",
        f"Log level: {LOG_LEVEL}",
        f"Rate limit: {'enabled' if RATE_LIMIT_ENABLED else 'disabled'}",
        f"Notifications: {NOTIFICATION_PROVIDER}",
    ]
    for line in lines:
        log.info("config: %s", line)
    for issue in validate():
        log.warning("config: %s", issue)
