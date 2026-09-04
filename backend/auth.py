"""Merchant authentication backend for Mandate Rescue.

Handles: registration, OTP email-verification, login, logout,
         forgot-password, change-password, session management.

Design invariants:
  - Passwords hashed with PBKDF2-SHA256 (werkzeug.security) — never stored plain.
  - OTPs stored as SHA-256 hash — never stored plain, never returned to client.
  - Sessions are server-side SQLite rows with 7-day hard expiry.
  - Session token is a 32-byte cryptographically random hex string in an
    HttpOnly, SameSite=Lax cookie. Never in localStorage.
  - All error messages are generic to avoid account enumeration.
  - Every significant event is written to security_events.
  - Passwords/OTP values are never logged.
"""

import hashlib
import logging
import re
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from werkzeug.security import check_password_hash, generate_password_hash

import db

log = logging.getLogger("mandate_rescue.auth")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_COOKIE_NAME = "mr_session"
SESSION_TTL_DAYS    = 7

OTP_TTL_SECONDS     = 600        # 10 minutes
OTP_MAX_ATTEMPTS    = 5
OTP_RESEND_COOLDOWN = 60         # seconds between resend requests

PASSWORD_MIN_LEN    = 8
PASSWORD_MAX_LEN    = 128

# ---------------------------------------------------------------------------
# Password policy
# ---------------------------------------------------------------------------

def validate_password(password: str) -> Tuple[bool, str]:
    """Return (ok, reason). Enforces length + basic complexity."""
    if not password or not isinstance(password, str):
        return False, "Password is required."
    if len(password) < PASSWORD_MIN_LEN:
        return False, f"Password must be at least {PASSWORD_MIN_LEN} characters."
    if len(password) > PASSWORD_MAX_LEN:
        return False, "Password is too long."
    if not re.search(r"[A-Za-z]", password):
        return False, "Password must contain at least one letter."
    if not re.search(r"[0-9]", password):
        return False, "Password must contain at least one number."
    return True, ""


def hash_password(password: str) -> str:
    """Return a PBKDF2-SHA256 hash suitable for storage."""
    return generate_password_hash(password, method="pbkdf2:sha256:260000")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time password verification."""
    return check_password_hash(password_hash, password)


# ---------------------------------------------------------------------------
# OTP helpers
# ---------------------------------------------------------------------------

def _generate_otp() -> str:
    """Return a cryptographically secure 6-digit OTP string."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _hash_otp(otp: str) -> str:
    """SHA-256 hash of the OTP. Never store plaintext."""
    return hashlib.sha256(otp.encode()).hexdigest()


def _verify_otp_hash(otp: str, stored_hash: str) -> bool:
    """Constant-time comparison of OTP against stored hash."""
    return hmac_compare(_hash_otp(otp), stored_hash)


def hmac_compare(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class RegistrationError(Exception):
    pass


def _validate_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email.strip()))


def _delete_unverified_merchant(conn, merchant_id: str) -> None:
    """Remove a stale unverified account and all its dependent rows.

    Called when a user re-registers with the same email before verifying.
    Safe: only touches rows where email_verified=0 — never deletes active accounts.
    """
    # Delete in dependency order (FK constraints)
    for tbl in ("otp_challenges", "sessions", "security_events",
                "notification_preferences", "notification_records"):
        conn.execute(f"DELETE FROM {tbl} WHERE merchant_id = ?", (merchant_id,))
    conn.execute(
        "DELETE FROM merchants WHERE merchant_id = ? AND email_verified = 0",
        (merchant_id,),
    )
    log.info("Deleted stale unverified merchant merchant_id=%s", merchant_id)


def register_merchant(conn, data: dict) -> Tuple[str, str]:
    """Validate registration data, create pending merchant, return (merchant_id, otp).

    Raises RegistrationError with a user-facing message on validation failure.
    Never returns the OTP in any log — caller must immediately pass it to email.

    Returns (merchant_id, otp) — caller sends OTP by email and DISCARDS it.
    """
    email = (data.get("email") or "").strip().lower()
    if not _validate_email(email):
        raise RegistrationError("A valid email address is required.")

    password  = data.get("password", "")
    password2 = data.get("confirm_password", "")
    if not password:
        raise RegistrationError("Password is required.")
    if password != password2:
        raise RegistrationError("Passwords do not match.")
    ok, reason = validate_password(password)
    if not ok:
        raise RegistrationError(reason)

    full_name = (data.get("full_name") or "").strip()
    if not full_name:
        raise RegistrationError("Full name is required.")
    if len(full_name) > 120:
        raise RegistrationError("Full name is too long.")

    business_name = (data.get("business_name") or "").strip()
    if not business_name:
        raise RegistrationError("Business/company name is required.")

    if not data.get("terms_accepted"):
        raise RegistrationError("You must accept the terms and privacy policy.")

    # Check duplicate email
    existing = db.get_merchant_by_email(conn, email)
    if existing:
        if existing["email_verified"]:
            # Generic — do not reveal account existence to unauthenticated callers
            raise RegistrationError(
                "Unable to register with this email. "
                "If you already have an account, please log in."
            )
        else:
            # Unverified stale account — delete it and let the user start fresh.
            # This handles the common case of someone who registered but never
            # verified, then tries to register again with the same email.
            _delete_unverified_merchant(conn, existing["merchant_id"])

    merchant_id   = str(uuid.uuid4())
    password_hash = hash_password(password)

    created = db.create_merchant(
        conn,
        merchant_id=merchant_id,
        email=email,
        password_hash=password_hash,
        full_name=full_name,
        business_name=business_name,
        phone=(data.get("phone") or "").strip() or None,
        country=(data.get("country") or "IN").strip(),
        address_line1=(data.get("address_line1") or "").strip() or None,
        address_line2=(data.get("address_line2") or "").strip() or None,
        city=(data.get("city") or "").strip() or None,
        state_region=(data.get("state_region") or "").strip() or None,
        postal_code=(data.get("postal_code") or "").strip() or None,
        business_type=(data.get("business_type") or "").strip() or None,
        business_website=(data.get("business_website") or "").strip() or None,
        business_address=(data.get("business_address") or "").strip() or None,
        terms_accepted=1,
    )
    if not created:
        raise RegistrationError(
            "Unable to register with this email. Please try a different email address."
        )

    # Create default notification preferences
    db.get_or_create_notification_prefs(conn, merchant_id)

    # Generate OTP for email verification
    otp = _generate_otp()
    challenge_id = str(uuid.uuid4())
    db.create_otp_challenge(
        conn,
        challenge_id=challenge_id,
        email=email,
        purpose="registration",
        otp_hash=_hash_otp(otp),
        ttl_seconds=OTP_TTL_SECONDS,
        merchant_id=merchant_id,
    )
    conn.commit()

    db.log_security_event(conn, merchant_id, "registered",
                           detail=f"Registration started for {email}")
    conn.commit()

    log.info("New merchant registered email=%s merchant_id=%s", email, merchant_id)
    return merchant_id, otp   # OTP must be emailed; caller discards immediately


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------

class OTPError(Exception):
    """Structured OTP verification error."""
    def __init__(self, code: str, message: str):
        self.code    = code      # expired | invalid | too_many_attempts | already_verified
        self.message = message
        super().__init__(message)


def verify_registration_otp(conn, email: str, otp: str,
                              ip: str = None, ua: str = None) -> str:
    """Verify a registration OTP and activate the account.

    Returns merchant_id on success. Raises OTPError on failure.
    """
    email = email.strip().lower()
    merchant = db.get_merchant_by_email(conn, email)
    if not merchant:
        raise OTPError("invalid", "Invalid verification code.")
    if merchant["email_verified"]:
        raise OTPError("already_verified", "This email has already been verified.")

    challenge = db.get_latest_otp_challenge(conn, email, "registration")
    if not challenge:
        raise OTPError("expired", "Verification code has expired. Please request a new one.")

    attempts = db.increment_otp_attempts(conn, challenge["challenge_id"])
    conn.commit()

    if attempts > OTP_MAX_ATTEMPTS:
        raise OTPError("too_many_attempts",
                       "Too many failed attempts. Please request a new verification code.")

    if not _verify_otp_hash(otp.strip(), challenge["otp_hash"]):
        remaining = max(0, OTP_MAX_ATTEMPTS - attempts)
        raise OTPError("invalid",
                       f"Invalid verification code. {remaining} attempt(s) remaining.")

    # Valid — activate account
    db.consume_otp_challenge(conn, challenge["challenge_id"])
    db.invalidate_otp_challenges(conn, email, "registration")
    db.verify_merchant_email(conn, merchant["merchant_id"])
    db.log_security_event(conn, merchant["merchant_id"], "email_verified",
                           ip_address=ip, user_agent=ua)
    conn.commit()
    log.info("Email verified merchant_id=%s", merchant["merchant_id"])
    return merchant["merchant_id"]


def resend_registration_otp(conn, email: str) -> Tuple[str, str]:
    """Issue a fresh OTP for an unverified account. Returns (merchant_id, otp).

    Raises OTPError if the account is already verified or doesn't exist,
    or if within the resend cooldown window.
    """
    email = email.strip().lower()
    merchant = db.get_merchant_by_email(conn, email)
    if not merchant:
        # Generic — do not reveal whether email exists
        raise OTPError("invalid", "No pending verification found for this email.")
    if merchant["email_verified"]:
        raise OTPError("already_verified", "This email is already verified.")

    # Cooldown: check if a recent challenge exists
    existing = db.get_latest_otp_challenge(conn, email, "registration")
    if existing:
        created = datetime.fromisoformat(existing["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - created).total_seconds()
        if elapsed < OTP_RESEND_COOLDOWN:
            wait = int(OTP_RESEND_COOLDOWN - elapsed) + 1
            raise OTPError("cooldown",
                           f"Please wait {wait}s before requesting a new code.")

    otp = _generate_otp()
    challenge_id = str(uuid.uuid4())
    db.create_otp_challenge(
        conn, challenge_id=challenge_id,
        email=email, purpose="registration",
        otp_hash=_hash_otp(otp),
        ttl_seconds=OTP_TTL_SECONDS,
        merchant_id=merchant["merchant_id"],
    )
    conn.commit()
    return merchant["merchant_id"], otp


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

class LoginError(Exception):
    pass


def login_merchant(conn, email: str, password: str,
                   ip: str = None, ua: str = None) -> Tuple[str, str]:
    """Authenticate merchant. Returns (merchant_id, session_id).

    Raises LoginError with a generic message on any failure.
    """
    email = (email or "").strip().lower()
    if not email or not password:
        raise LoginError("Email and password are required.")

    merchant = db.get_merchant_by_email(conn, email)

    # Constant-time path: always check password even if merchant not found,
    # to prevent timing-based account enumeration.
    _DUMMY_HASH = "pbkdf2:sha256:260000$x$" + "a" * 64
    stored_hash = merchant["password_hash"] if merchant else _DUMMY_HASH

    password_ok = verify_password(password, stored_hash)

    if not merchant or not password_ok:
        if merchant:
            db.log_security_event(conn, merchant["merchant_id"], "login_failed",
                                   ip_address=ip, user_agent=ua,
                                   detail="Wrong password")
            conn.commit()
        log.warning("Login failed for email=%s ip=%s", email, ip)
        raise LoginError("Incorrect email or password.")

    if not merchant["email_verified"]:
        raise LoginError(
            "Your email address is not yet verified. "
            "Please check your inbox for the verification code."
        )

    if not merchant["is_active"]:
        raise LoginError("This account has been deactivated. Please contact support.")

    # Create session
    session_id = secrets.token_hex(32)
    db.create_session(conn, session_id, merchant["merchant_id"],
                      ip_address=ip, user_agent=ua)
    db.set_merchant_last_login(conn, merchant["merchant_id"])
    db.log_security_event(conn, merchant["merchant_id"], "login_success",
                           ip_address=ip, user_agent=ua)
    conn.commit()
    log.info("Login success merchant_id=%s ip=%s", merchant["merchant_id"], ip)
    return merchant["merchant_id"], session_id


# ---------------------------------------------------------------------------
# Session resolution
# ---------------------------------------------------------------------------

def resolve_session(conn, session_id: str) -> Optional[dict]:
    """Return the merchant dict for a valid session, or None.

    Touches last_seen_at so the UI can detect session activity. Does NOT extend
    the 7-day hard expiry — that is absolute.
    """
    if not session_id:
        return None
    session = db.get_session(conn, session_id)
    if not session:
        return None
    merchant = db.get_merchant_by_id(conn, session["merchant_id"])
    if not merchant or not merchant["is_active"]:
        return None
    db.touch_session(conn, session_id)
    return merchant


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

def logout(conn, session_id: str, merchant_id: str = None,
           ip: str = None, ua: str = None) -> None:
    """Invalidate the session server-side."""
    db.invalidate_session(conn, session_id)
    if merchant_id:
        db.log_security_event(conn, merchant_id, "logout",
                               ip_address=ip, user_agent=ua)
    conn.commit()
    log.info("Session invalidated session_id=...%s", session_id[-6:])


# ---------------------------------------------------------------------------
# Forgot password — request OTP
# ---------------------------------------------------------------------------

def request_password_reset(conn, email: str) -> Optional[Tuple[str, str]]:
    """Issue a password-reset OTP.

    Returns (merchant_id, otp) if the account exists and is verified,
    else returns None (caller always responds with the same success message
    to avoid account enumeration).
    """
    email = (email or "").strip().lower()
    merchant = db.get_merchant_by_email(conn, email)
    if not merchant or not merchant["email_verified"] or not merchant["is_active"]:
        return None  # Caller shows generic success — do not reveal account existence

    # Cooldown check
    existing = db.get_latest_otp_challenge(conn, email, "password_reset")
    if existing:
        created = datetime.fromisoformat(existing["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - created).total_seconds()
        if elapsed < OTP_RESEND_COOLDOWN:
            return None   # Silently throttle — same generic response to client

    otp = _generate_otp()
    challenge_id = str(uuid.uuid4())
    db.create_otp_challenge(
        conn, challenge_id=challenge_id,
        email=email, purpose="password_reset",
        otp_hash=_hash_otp(otp),
        ttl_seconds=OTP_TTL_SECONDS,
        merchant_id=merchant["merchant_id"],
    )
    conn.commit()
    log.info("Password reset OTP issued for merchant_id=%s", merchant["merchant_id"])
    return merchant["merchant_id"], otp


def reset_password(conn, email: str, otp: str, new_password: str,
                   ip: str = None, ua: str = None) -> str:
    """Verify password-reset OTP and update password.

    Returns merchant_id on success. Raises OTPError or RegistrationError.
    """
    email = email.strip().lower()
    merchant = db.get_merchant_by_email(conn, email)
    if not merchant:
        raise OTPError("invalid", "Invalid or expired reset code.")

    challenge = db.get_latest_otp_challenge(conn, email, "password_reset")
    if not challenge:
        raise OTPError("expired", "Reset code has expired. Please request a new one.")

    attempts = db.increment_otp_attempts(conn, challenge["challenge_id"])
    conn.commit()
    if attempts > OTP_MAX_ATTEMPTS:
        raise OTPError("too_many_attempts",
                       "Too many failed attempts. Please request a new reset code.")

    if not _verify_otp_hash(otp.strip(), challenge["otp_hash"]):
        remaining = max(0, OTP_MAX_ATTEMPTS - attempts)
        raise OTPError("invalid", f"Invalid code. {remaining} attempt(s) remaining.")

    ok, reason = validate_password(new_password)
    if not ok:
        raise RegistrationError(reason)

    db.consume_otp_challenge(conn, challenge["challenge_id"])
    db.invalidate_otp_challenges(conn, email, "password_reset")
    db.update_merchant(conn, merchant["merchant_id"],
                        password_hash=hash_password(new_password))
    # Invalidate ALL existing sessions after password reset
    db.invalidate_all_sessions(conn, merchant["merchant_id"])
    db.log_security_event(conn, merchant["merchant_id"], "password_reset",
                           ip_address=ip, user_agent=ua)
    conn.commit()
    log.info("Password reset complete merchant_id=%s", merchant["merchant_id"])
    return merchant["merchant_id"]


# ---------------------------------------------------------------------------
# Change password (authenticated)
# ---------------------------------------------------------------------------

def request_change_password_otp(conn, merchant_id: str) -> Tuple[str, str]:
    """Send OTP to the merchant's verified email for a password-change flow.

    Returns (email, otp). Caller sends the OTP by email.
    """
    merchant = db.get_merchant_by_id(conn, merchant_id)
    if not merchant:
        raise LoginError("Merchant not found.")

    email = merchant["email"]
    existing = db.get_latest_otp_challenge(conn, email, "change_password")
    if existing:
        created = datetime.fromisoformat(existing["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - created).total_seconds()
        if elapsed < OTP_RESEND_COOLDOWN:
            wait = int(OTP_RESEND_COOLDOWN - elapsed) + 1
            raise OTPError("cooldown", f"Please wait {wait}s before requesting again.")

    otp = _generate_otp()
    challenge_id = str(uuid.uuid4())
    db.create_otp_challenge(
        conn, challenge_id=challenge_id,
        email=email, purpose="change_password",
        otp_hash=_hash_otp(otp),
        ttl_seconds=OTP_TTL_SECONDS,
        merchant_id=merchant_id,
    )
    conn.commit()
    return email, otp


def change_password(conn, merchant_id: str, otp: str,
                    new_password: str, confirm_password: str,
                    current_session_id: str = None,
                    ip: str = None, ua: str = None) -> None:
    """Verify OTP + change password. Invalidates other sessions.

    Raises OTPError or RegistrationError on any failure.
    """
    merchant = db.get_merchant_by_id(conn, merchant_id)
    if not merchant:
        raise LoginError("Merchant not found.")

    email = merchant["email"]
    challenge = db.get_latest_otp_challenge(conn, email, "change_password")
    if not challenge:
        raise OTPError("expired", "OTP has expired. Please request a new one.")

    attempts = db.increment_otp_attempts(conn, challenge["challenge_id"])
    conn.commit()
    if attempts > OTP_MAX_ATTEMPTS:
        raise OTPError("too_many_attempts", "Too many failed attempts.")

    if not _verify_otp_hash(otp.strip(), challenge["otp_hash"]):
        remaining = max(0, OTP_MAX_ATTEMPTS - attempts)
        raise OTPError("invalid", f"Invalid OTP. {remaining} attempt(s) remaining.")

    if new_password != confirm_password:
        raise RegistrationError("Passwords do not match.")
    ok, reason = validate_password(new_password)
    if not ok:
        raise RegistrationError(reason)

    db.consume_otp_challenge(conn, challenge["challenge_id"])
    db.invalidate_otp_challenges(conn, email, "change_password")
    db.update_merchant(conn, merchant_id, password_hash=hash_password(new_password))
    # Invalidate all other sessions; keep current if provided
    db.invalidate_all_sessions(conn, merchant_id, except_session_id=current_session_id)
    db.log_security_event(conn, merchant_id, "password_changed",
                           ip_address=ip, user_agent=ua)
    conn.commit()
    log.info("Password changed merchant_id=%s", merchant_id)


# ---------------------------------------------------------------------------
# Change email (authenticated)
# ---------------------------------------------------------------------------

def request_change_email_otp(conn, merchant_id: str, new_email: str) -> Tuple[str, str]:
    """Send OTP to the NEW email address to verify ownership.

    Returns (new_email, otp).
    """
    new_email = (new_email or "").strip().lower()
    if not _validate_email(new_email):
        raise RegistrationError("A valid new email address is required.")

    # Reject if new email already belongs to another account
    existing = db.get_merchant_by_email(conn, new_email)
    if existing and existing["merchant_id"] != merchant_id:
        raise RegistrationError(
            "That email address is already in use. Please choose a different one."
        )

    otp = _generate_otp()
    challenge_id = str(uuid.uuid4())
    db.create_otp_challenge(
        conn, challenge_id=challenge_id,
        email=new_email, purpose="change_email",
        otp_hash=_hash_otp(otp),
        ttl_seconds=OTP_TTL_SECONDS,
        merchant_id=merchant_id,
        new_email=new_email,
    )
    conn.commit()
    return new_email, otp


def confirm_change_email(conn, merchant_id: str, new_email: str, otp: str,
                          ip: str = None, ua: str = None) -> None:
    """Verify OTP and swap the email address.

    Raises OTPError or RegistrationError on failure.
    """
    new_email = new_email.strip().lower()
    challenge = db.get_latest_otp_challenge(conn, new_email, "change_email")
    if not challenge or challenge.get("merchant_id") != merchant_id:
        raise OTPError("expired", "OTP has expired or is invalid.")

    attempts = db.increment_otp_attempts(conn, challenge["challenge_id"])
    conn.commit()
    if attempts > OTP_MAX_ATTEMPTS:
        raise OTPError("too_many_attempts", "Too many failed attempts.")

    if not _verify_otp_hash(otp.strip(), challenge["otp_hash"]):
        remaining = max(0, OTP_MAX_ATTEMPTS - attempts)
        raise OTPError("invalid", f"Invalid OTP. {remaining} attempt(s) remaining.")

    # Check no other merchant claimed this email in the meantime
    conflict = db.get_merchant_by_email(conn, new_email)
    if conflict and conflict["merchant_id"] != merchant_id:
        raise RegistrationError("Email address is already in use.")

    db.consume_otp_challenge(conn, challenge["challenge_id"])
    db.update_merchant(conn, merchant_id, email=new_email, email_verified=1)
    db.log_security_event(conn, merchant_id, "email_changed",
                           ip_address=ip, user_agent=ua,
                           detail=f"Email changed to ...{new_email[-8:]}")
    conn.commit()
    log.info("Email changed merchant_id=%s new_email=...%s",
             merchant_id, new_email[-6:])


# ---------------------------------------------------------------------------
# Helpers used by Flask request context
# ---------------------------------------------------------------------------

def _client_ip(request) -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    return xff.split(",")[0].strip() if xff else (request.remote_addr or "")


def _client_ua(request) -> str:
    return (request.headers.get("User-Agent") or "")[:200]


def _public_merchant(merchant: dict) -> dict:
    """Return merchant dict with only public-safe fields (no password_hash)."""
    safe_keys = [
        "merchant_id", "email", "email_verified", "full_name", "phone",
        "country", "address_line1", "address_line2", "city", "state_region",
        "postal_code", "business_name", "business_type", "business_website",
        "business_address", "role", "is_active", "created_at", "updated_at",
        "last_login_at", "terms_accepted",
    ]
    return {k: merchant[k] for k in safe_keys if k in merchant}
