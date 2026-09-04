"""Email service for Mandate Rescue — merchant notifications and OTP delivery.

Architecture:
    EmailService
        → EmailProvider (abstract)
            → GoogleSMTPProvider  (real SMTP via Google Workspace / Gmail App Password)
            → SimulatedProvider   (dev/CI mode — logs OTP to server, marks SIMULATED)

Design invariants:
  - Never claim SENT unless the SMTP server accepted the message (250 OK).
  - Never log OTP values — only log "OTP email queued for <masked email>".
  - Never log SMTP credentials.
  - Provider is selected by NOTIFICATION_EMAIL_PROVIDER env var:
        "google_smtp"  → real Google SMTP
        "simulated"    → default/CI mode
  - In simulated mode the OTP is printed to server log at WARNING level so
    developers can verify flows without a real mailbox.
  - Both HTML and plain-text bodies are sent as a multipart/alternative message.

Google SMTP setup (document in README):
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USERNAME=your.address@gmail.com
  SMTP_PASSWORD=your_app_password    # Google App Password (not your Gmail login)
  MAIL_FROM=your.address@gmail.com
  MAIL_FROM_NAME=Mandate Rescue

  Enable 2FA on the Gmail account, then create an App Password at
  https://myaccount.google.com/apppasswords
"""

import logging
import os
import smtplib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import db

log = logging.getLogger("mandate_rescue.email_service")

# ---------------------------------------------------------------------------
# Configuration helpers — read from env at call time, not import time.
# This ensures .env values loaded by dotenv after import are always picked up.
# ---------------------------------------------------------------------------

def _smtp_host():        return os.environ.get("SMTP_HOST", "smtp.gmail.com")
def _smtp_port():        return int(os.environ.get("SMTP_PORT", "587"))
def _smtp_username():    return os.environ.get("SMTP_USERNAME", "").strip()
def _smtp_password():
    # Google App Passwords are shown with spaces (e.g. "acza hiwc gzbn arew")
    # but must be sent without any spaces. Always strip them.
    return os.environ.get("SMTP_PASSWORD", "").replace(" ", "").replace("\t", "")
def _mail_from():        return (os.environ.get("MAIL_FROM", "") or _smtp_username()).strip()
def _mail_from_name():   return os.environ.get("MAIL_FROM_NAME", "Mandate Rescue").strip()
def _email_provider():   return os.environ.get("NOTIFICATION_EMAIL_PROVIDER", "simulated").lower().strip()

# Keep module-level names for backwards compatibility (read at import for non-critical uses)
SMTP_HOST       = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT       = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME   = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD   = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM       = os.environ.get("MAIL_FROM", SMTP_USERNAME)
MAIL_FROM_NAME  = os.environ.get("MAIL_FROM_NAME", "Mandate Rescue")
EMAIL_PROVIDER  = os.environ.get("NOTIFICATION_EMAIL_PROVIDER", "simulated").lower()

# ---------------------------------------------------------------------------
# Email result
# ---------------------------------------------------------------------------

class EmailStatus:
    SENT       = "SENT"
    FAILED     = "FAILED"
    SIMULATED  = "SIMULATED"


@dataclass
class EmailResult:
    status:     str
    provider:   str
    recipient_masked: str
    subject:    str
    email_type: str
    error:      Optional[str] = None
    sent_at:    str = field(default_factory=lambda:
        datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return {
            "status":           self.status,
            "provider":         self.provider,
            "recipient_masked": self.recipient_masked,
            "subject":          self.subject,
            "email_type":       self.email_type,
            "error":            self.error,
            "sent_at":          self.sent_at,
        }


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------

class EmailProvider:
    name: str = "base"

    def send(self, to: str, subject: str, html_body: str,
             text_body: str) -> EmailResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Google SMTP provider
# ---------------------------------------------------------------------------

class GoogleSMTPProvider(EmailProvider):
    """Sends email via Google SMTP (TLS on port 587).

    Credentials are read from environment at SEND time, not import time,
    so .env values loaded by dotenv after module import are always picked up.

    Requires SMTP_USERNAME and SMTP_PASSWORD (Google App Password).
    Raises ValueError at construction time if credentials are missing.
    """
    name = "google_smtp"

    def __init__(self):
        # Validate at construction — but read live in case dotenv ran after import
        if not _smtp_username() or not _smtp_password():
            raise ValueError(
                "GoogleSMTPProvider requires SMTP_USERNAME and SMTP_PASSWORD. "
                "Set them in .env (use a Google App Password, not your Gmail login)."
            )

    def send(self, to: str, subject: str, html_body: str,
             text_body: str) -> EmailResult:
        # Re-read from env at send time so credentials set after module import work.
        username  = _smtp_username()
        password  = _smtp_password()
        host      = _smtp_host()
        port      = _smtp_port()
        from_addr = _mail_from()
        from_name = _mail_from_name()

        masked = _mask_email(to)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{from_name} <{from_addr}>"
        msg["To"]      = to
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html",  "utf-8"))

        try:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(username, password)
                smtp.sendmail(from_addr, [to], msg.as_string())
            log.info("Email SENT via google_smtp to %s subject=%r", masked, subject)
            return EmailResult(
                status=EmailStatus.SENT, provider=self.name,
                recipient_masked=masked, subject=subject,
                email_type="",
            )
        except smtplib.SMTPAuthenticationError as exc:
            log.error("SMTP auth failed (check SMTP_PASSWORD / App Password): %s", exc)
            return EmailResult(
                status=EmailStatus.FAILED, provider=self.name,
                recipient_masked=masked, subject=subject,
                email_type="",
                error="SMTP authentication failed. Check SMTP_PASSWORD (must be a Google App Password).",
            )
        except smtplib.SMTPException as exc:
            log.error("SMTP error sending to %s: %s", masked, exc)
            return EmailResult(
                status=EmailStatus.FAILED, provider=self.name,
                recipient_masked=masked, subject=subject, email_type="",
                error=f"SMTP error: {type(exc).__name__}: {exc}",
            )
        except OSError as exc:
            log.error("Network error sending email to %s: %s", masked, exc)
            return EmailResult(
                status=EmailStatus.FAILED, provider=self.name,
                recipient_masked=masked, subject=subject, email_type="",
                error=f"Network error: {type(exc).__name__}",
            )


# ---------------------------------------------------------------------------
# Simulated provider (development / CI)
# ---------------------------------------------------------------------------

class SimulatedEmailProvider(EmailProvider):
    """Logs email to server log. Never sends real mail. Marks status SIMULATED.

    In dev mode the OTP appears in the server log at WARNING level so developers
    can complete flows. This NEVER happens with a real provider.
    """
    name = "simulated"

    def send(self, to: str, subject: str, html_body: str,
             text_body: str) -> EmailResult:
        masked = _mask_email(to)
        # Log the plain-text body so devs can find the OTP in the server log.
        # In a real deployment this provider is never active.
        log.warning(
            "[SIMULATED EMAIL] To: %s | Subject: %s\n--- body ---\n%s\n---",
            masked, subject, text_body[:1200],
        )
        return EmailResult(
            status=EmailStatus.SIMULATED, provider=self.name,
            recipient_masked=masked, subject=subject, email_type="",
        )


# ---------------------------------------------------------------------------
# Email templates
# ---------------------------------------------------------------------------

_BRAND = "Mandate Rescue"
_SUPPORT = "support@mandaterescue.io"  # placeholder


def _base_html(title: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  body{{font-family:Inter,Arial,sans-serif;background:#0f111a;color:#e2e8f0;margin:0;padding:0}}
  .wrap{{max-width:560px;margin:40px auto;background:#1a1d2e;border-radius:12px;overflow:hidden}}
  .head{{background:#6366f1;padding:28px 32px}}
  .head h1{{margin:0;font-size:20px;color:#fff;font-weight:700}}
  .head p{{margin:4px 0 0;color:rgba(255,255,255,.7);font-size:13px}}
  .body{{padding:32px}}
  .otp-box{{background:#0f111a;border:1px solid #2d3148;border-radius:8px;
            padding:24px;text-align:center;margin:24px 0}}
  .otp-code{{font-family:JetBrains Mono,monospace;font-size:36px;font-weight:700;
             color:#6366f1;letter-spacing:8px}}
  .otp-note{{font-size:12px;color:#64748b;margin-top:8px}}
  .btn{{display:inline-block;background:#6366f1;color:#fff;padding:12px 28px;
        border-radius:8px;text-decoration:none;font-weight:600;margin:16px 0}}
  .divider{{border:none;border-top:1px solid #2d3148;margin:24px 0}}
  .footer{{font-size:11px;color:#475569;padding:0 32px 24px;text-align:center}}
  .alert-box{{background:#1e1a2e;border-left:3px solid #f59e0b;padding:16px;
              border-radius:0 8px 8px 0;margin:16px 0}}
  p{{line-height:1.6;color:#cbd5e1}}
  strong{{color:#e2e8f0}}
</style>
</head>
<body>
<div class="wrap">
  <div class="head">
    <h1>⚡ {_BRAND}</h1>
    <p>UPI Autopay Recovery Platform</p>
  </div>
  <div class="body">
    {body_html}
  </div>
  <hr class="divider">
  <div class="footer">
    <p>This email was sent from {_BRAND}. Do not reply to this email.<br>
    If you did not request this, please ignore it or contact
    <a href="mailto:{_SUPPORT}" style="color:#6366f1">{_SUPPORT}</a>.</p>
  </div>
</div>
</body>
</html>"""


def _otp_html_body(title: str, greeting: str, intro: str,
                    otp: str, expiry_min: int, extra: str = "") -> str:
    return f"""
<h2 style="margin:0 0 16px;color:#e2e8f0;font-size:20px">{title}</h2>
<p>{greeting}</p>
<p>{intro}</p>
<div class="otp-box">
  <div class="otp-code">{otp}</div>
  <div class="otp-note">Valid for {expiry_min} minutes · One-time use only</div>
</div>
{extra}
<p>If you did not request this code, you can safely ignore this email.</p>
<p style="color:#475569;font-size:12px">For security: never share this code with anyone, including {_BRAND} staff.</p>
"""


def _otp_text_body(title: str, greeting: str, intro: str,
                    otp: str, expiry_min: int) -> str:
    return f"""{_BRAND}
{title}
{'='*40}

{greeting}

{intro}

Your OTP: {otp}

Valid for {expiry_min} minutes. One-time use only.

If you did not request this code, ignore this email.
Never share your OTP with anyone.

-- {_BRAND} Team"""


class EmailTemplates:
    """Factory for all 9 email types.

    Each method returns (subject, html_body, text_body).
    OTPs are passed in as a parameter — this module never generates or stores them.
    """

    @staticmethod
    def registration_otp(full_name: str, otp: str) -> tuple:
        subject   = f"[{_BRAND}] Verify your email address"
        greeting  = f"Hi {full_name},"
        intro     = ("Thank you for registering with Mandate Rescue. "
                     "Use the code below to verify your email address and activate your account.")
        html_body = _base_html(subject, _otp_html_body(
            "Verify your email", greeting, intro, otp, 10))
        text_body = _otp_text_body("Verify your email", greeting, intro, otp, 10)
        return subject, html_body, text_body

    @staticmethod
    def password_reset_otp(full_name: str, otp: str) -> tuple:
        subject   = f"[{_BRAND}] Password reset code"
        greeting  = f"Hi {full_name},"
        intro     = ("We received a request to reset your Mandate Rescue password. "
                     "Use the code below to set a new password.")
        extra = ('<div class="alert-box">'
                 '<strong>Didn\'t request this?</strong> Your password has NOT been changed. '
                 'You can safely ignore this email.'
                 '</div>')
        html_body = _base_html(subject, _otp_html_body(
            "Reset your password", greeting, intro, otp, 10, extra))
        text_body = _otp_text_body("Reset your password", greeting, intro, otp, 10)
        return subject, html_body, text_body

    @staticmethod
    def change_password_otp(full_name: str, otp: str) -> tuple:
        subject   = f"[{_BRAND}] Confirm password change"
        greeting  = f"Hi {full_name},"
        intro     = ("You requested to change your Mandate Rescue password. "
                     "Enter the code below to confirm.")
        html_body = _base_html(subject, _otp_html_body(
            "Confirm password change", greeting, intro, otp, 10))
        text_body = _otp_text_body("Confirm password change", greeting, intro, otp, 10)
        return subject, html_body, text_body

    @staticmethod
    def change_email_otp(full_name: str, new_email: str, otp: str) -> tuple:
        subject   = f"[{_BRAND}] Verify your new email address"
        greeting  = f"Hi {full_name},"
        intro     = (f"You requested to change your email address to "
                     f"<strong>{new_email}</strong>. "
                     "Enter the code below to confirm.")
        html_body = _base_html(subject, _otp_html_body(
            "Verify new email", greeting, intro, otp, 10))
        text_body = _otp_text_body("Verify new email", greeting, intro, otp, 10)
        return subject, html_body, text_body

    @staticmethod
    def login_security_alert(full_name: str, ip: str, ua: str,
                              timestamp: str) -> tuple:
        subject = f"[{_BRAND}] New login to your account"
        body_html = f"""
<h2 style="margin:0 0 16px;color:#e2e8f0;font-size:20px">New login detected</h2>
<p>Hi {full_name},</p>
<p>A new login was detected on your Mandate Rescue account.</p>
<div class="alert-box">
  <strong>Time:</strong> {timestamp}<br>
  <strong>IP Address:</strong> {ip or 'Unknown'}<br>
  <strong>Device:</strong> {(ua or 'Unknown')[:80]}
</div>
<p>If this was you, no action is needed.</p>
<p>If you did not log in, please <a href="mailto:{_SUPPORT}" style="color:#6366f1">
contact support</a> immediately and change your password.</p>
"""
        body_text = f"""New login to your Mandate Rescue account.

Time: {timestamp}
IP: {ip or 'Unknown'}
Device: {(ua or 'Unknown')[:80]}

If this was not you, contact support immediately.
"""
        return subject, _base_html(subject, body_html), body_text

    @staticmethod
    def recovery_escalation(business_name: str, customer_id: str,
                             amount: int, reason: str) -> tuple:
        subject = f"[{_BRAND}] Recovery escalation — customer {customer_id}"
        body_html = f"""
<h2 style="margin:0 0 16px;color:#e2e8f0;font-size:20px">Recovery Escalation Alert</h2>
<p>Hi {business_name},</p>
<p>A recovery case has been escalated and requires your attention.</p>
<div class="alert-box">
  <strong>Customer ID:</strong> {customer_id}<br>
  <strong>Amount:</strong> ₹{amount:,}<br>
  <strong>Reason:</strong> {reason}
</div>
<p>Please log in to Mandate Rescue to review and take action.</p>
"""
        body_text = f"""Recovery Escalation — {business_name}

Customer: {customer_id}
Amount: Rs {amount}
Reason: {reason}

Log in to Mandate Rescue to review.
"""
        return subject, _base_html(subject, body_html), body_text

    @staticmethod
    def anomaly_alert(business_name: str, alert_type: str,
                       detail: str) -> tuple:
        subject = f"[{_BRAND}] Anomaly detected — {alert_type}"
        body_html = f"""
<h2 style="margin:0 0 16px;color:#e2e8f0;font-size:20px">Anomaly Alert</h2>
<p>Hi {business_name},</p>
<p>Mandate Rescue detected an anomaly in your payment recovery data.</p>
<div class="alert-box">
  <strong>Type:</strong> {alert_type}<br>
  <strong>Detail:</strong> {detail}
</div>
<p>Log in to review the intelligence dashboard for more information.</p>
"""
        body_text = f"""Anomaly Alert — {business_name}

Type: {alert_type}
Detail: {detail}

Log in to review.
"""
        return subject, _base_html(subject, body_html), body_text

    @staticmethod
    def policy_recommendation(business_name: str, title: str,
                               summary: str) -> tuple:
        subject = f"[{_BRAND}] New policy recommendation"
        body_html = f"""
<h2 style="margin:0 0 16px;color:#e2e8f0;font-size:20px">Policy Recommendation</h2>
<p>Hi {business_name},</p>
<p>Mandate Rescue has a new data-backed recovery policy recommendation for you.</p>
<div class="alert-box">
  <strong>{title}</strong><br>
  {summary}
</div>
<p>Log in to review the recommendation and approve or reject it.</p>
"""
        body_text = f"""Policy Recommendation — {business_name}

{title}
{summary}

Log in to review.
"""
        return subject, _base_html(subject, body_html), body_text

    @staticmethod
    def recovery_failure_alert(business_name: str, customer_id: str,
                                amount: int) -> tuple:
        subject = f"[{_BRAND}] Recovery failed — action required"
        body_html = f"""
<h2 style="margin:0 0 16px;color:#e2e8f0;font-size:20px">Recovery Failed</h2>
<p>Hi {business_name},</p>
<p>All automated recovery attempts have been exhausted for a case.</p>
<div class="alert-box">
  <strong>Customer ID:</strong> {customer_id}<br>
  <strong>Amount at risk:</strong> ₹{amount:,}
</div>
<p>Manual intervention may be required. Log in to view the case details.</p>
"""
        body_text = f"""Recovery Failed — {business_name}

Customer: {customer_id}
Amount: Rs {amount}

Manual intervention may be required. Log in to view.
"""
        return subject, _base_html(subject, body_html), body_text

    @staticmethod
    def test_email(full_name: str) -> tuple:
        subject = f"[{_BRAND}] Test email"
        body_html = f"""
<h2 style="margin:0 0 16px;color:#e2e8f0;font-size:20px">Test Email ✓</h2>
<p>Hi {full_name},</p>
<p>Your Mandate Rescue email configuration is working correctly.</p>
<p style="color:#64748b;font-size:13px">
  Sent at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
</p>
"""
        body_text = f"Test email from {_BRAND} — configuration is working. Hi {full_name}."
        return subject, _base_html(subject, body_html), body_text


# ---------------------------------------------------------------------------
# Email service — high-level dispatcher
# ---------------------------------------------------------------------------

class EmailService:
    """Sends transactional emails and persists delivery records.

    Use `get_email_service()` for the module-level singleton.
    Create a fresh instance (with a specific provider) for tests.
    """

    def __init__(self, provider: EmailProvider = None):
        self._provider = provider or _make_default_provider()

    @property
    def provider_name(self) -> str:
        return self._provider.name

    @property
    def is_real(self) -> bool:
        return self._provider.name != "simulated"

    def _send(self, conn, email_to: str, subject: str, html_body: str,
               text_body: str, email_type: str,
               merchant_id: str = None) -> EmailResult:
        """Internal send — logs record, calls provider, updates status."""
        record_id = str(uuid.uuid4())
        if conn:
            db.create_notification_record(
                conn, record_id=record_id,
                email_to=email_to, email_type=email_type,
                subject=subject, merchant_id=merchant_id,
                provider=self._provider.name,
            )
            # Update to SENDING before the actual call
            db.update_notification_record(conn, record_id, "SENDING")
            conn.commit()

        result = self._provider.send(email_to, subject, html_body, text_body)
        result.email_type = email_type

        if conn:
            db.update_notification_record(
                conn, record_id,
                status=result.status,
                failure_reason=result.error,
            )
            conn.commit()

        return result

    # --- OTP emails (always sent regardless of notification prefs) ---

    def send_registration_otp(self, conn, email: str, full_name: str,
                               otp: str, merchant_id: str = None) -> EmailResult:
        s, h, t = EmailTemplates.registration_otp(full_name, otp)
        result = self._send(conn, email, s, h, t, "registration_otp", merchant_id)
        log.info("registration_otp queued to %s status=%s", _mask_email(email), result.status)
        return result

    def send_password_reset_otp(self, conn, email: str, full_name: str,
                                 otp: str, merchant_id: str = None) -> EmailResult:
        s, h, t = EmailTemplates.password_reset_otp(full_name, otp)
        result = self._send(conn, email, s, h, t, "password_reset_otp", merchant_id)
        log.info("password_reset_otp queued to %s status=%s", _mask_email(email), result.status)
        return result

    def send_change_password_otp(self, conn, email: str, full_name: str,
                                  otp: str, merchant_id: str = None) -> EmailResult:
        s, h, t = EmailTemplates.change_password_otp(full_name, otp)
        result = self._send(conn, email, s, h, t, "change_password_otp", merchant_id)
        log.info("change_password_otp queued to %s status=%s", _mask_email(email), result.status)
        return result

    def send_change_email_otp(self, conn, new_email: str, full_name: str,
                               otp: str, merchant_id: str = None) -> EmailResult:
        s, h, t = EmailTemplates.change_email_otp(full_name, new_email, otp)
        result = self._send(conn, new_email, s, h, t, "change_email_otp", merchant_id)
        log.info("change_email_otp queued to %s status=%s", _mask_email(new_email), result.status)
        return result

    def send_login_alert(self, conn, email: str, full_name: str,
                          ip: str, ua: str, merchant_id: str = None) -> EmailResult:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        s, h, t = EmailTemplates.login_security_alert(full_name, ip, ua, ts)
        return self._send(conn, email, s, h, t, "login_alert", merchant_id)

    def send_recovery_escalation(self, conn, email: str, business_name: str,
                                  customer_id: str, amount: int, reason: str,
                                  merchant_id: str = None) -> EmailResult:
        s, h, t = EmailTemplates.recovery_escalation(business_name, customer_id, amount, reason)
        return self._send(conn, email, s, h, t, "recovery_escalation", merchant_id)

    def send_anomaly_alert(self, conn, email: str, business_name: str,
                            alert_type: str, detail: str,
                            merchant_id: str = None) -> EmailResult:
        s, h, t = EmailTemplates.anomaly_alert(business_name, alert_type, detail)
        return self._send(conn, email, s, h, t, "anomaly_alert", merchant_id)

    def send_policy_recommendation(self, conn, email: str, business_name: str,
                                    title: str, summary: str,
                                    merchant_id: str = None) -> EmailResult:
        s, h, t = EmailTemplates.policy_recommendation(business_name, title, summary)
        return self._send(conn, email, s, h, t, "policy_recommendation", merchant_id)

    def send_recovery_failure(self, conn, email: str, business_name: str,
                               customer_id: str, amount: int,
                               merchant_id: str = None) -> EmailResult:
        s, h, t = EmailTemplates.recovery_failure_alert(business_name, customer_id, amount)
        return self._send(conn, email, s, h, t, "recovery_failure", merchant_id)

    def send_test_email(self, conn, email: str, full_name: str,
                         merchant_id: str = None) -> EmailResult:
        s, h, t = EmailTemplates.test_email(full_name)
        return self._send(conn, email, s, h, t, "test_email", merchant_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mask_email(email: str) -> str:
    """Mask email for safe logging: a****@****.com"""
    try:
        local, domain = email.split("@", 1)
        parts = domain.rsplit(".", 1)
        d = parts[0] if len(parts) > 1 else domain
        tld = ("." + parts[1]) if len(parts) > 1 else ""
        return f"{local[0]}****@{'*'*(max(1,len(d)-2))}{d[-1] if d else ''}{tld}"
    except Exception:
        return "****@****"


def _make_default_provider() -> EmailProvider:
    """Select email provider.

    Priority:
    1. NOTIFICATION_EMAIL_PROVIDER=google_smtp  → use Google SMTP
    2. SMTP_USERNAME + SMTP_PASSWORD both set    → auto-use Google SMTP
    3. Otherwise                                 → SimulatedEmailProvider

    Credentials are read at call time so .env values loaded after import work.
    """
    provider_name = _email_provider()

    if provider_name == "google_smtp" or (_smtp_username() and _smtp_password()):
        try:
            return GoogleSMTPProvider()
        except ValueError as exc:
            log.warning(
                "GoogleSMTPProvider unavailable (%s); falling back to simulated.", exc
            )
    return SimulatedEmailProvider()


# Module-level singleton — tracks the provider name it was built for so it
# auto-refreshes when dotenv loads credentials after import.
_default_service: Optional[EmailService] = None
_default_service_provider: str = ""


def get_email_service() -> EmailService:
    """Return the configured EmailService, refreshing if env changed since last call.

    This handles the common case where dotenv loads SMTP credentials after
    email_service was first imported (e.g. app.py imports email_service, then
    load_dotenv() populates SMTP_USERNAME/SMTP_PASSWORD, then the first request
    calls get_email_service() — without this refresh logic it would still return
    the simulated provider from import time).
    """
    global _default_service, _default_service_provider
    current_provider = _email_provider()
    has_real_creds = bool(_smtp_username() and _smtp_password())

    # Refresh if: never built, provider env var changed, or credentials
    # have appeared since we last built with the simulated provider.
    need_refresh = (
        _default_service is None
        or current_provider != _default_service_provider
        or (has_real_creds and isinstance(
            getattr(_default_service, "_provider", None), SimulatedEmailProvider))
    )
    if need_refresh:
        _default_service = EmailService()
        _default_service_provider = current_provider
        log.info(
            "EmailService (re)initialised: provider=%s smtp_user=%s",
            _default_service.provider_name,
            _smtp_username() or "(none)",
        )
    return _default_service


def reset_email_service() -> None:
    """Force the singleton to be recreated on next get_email_service() call."""
    global _default_service, _default_service_provider
    _default_service = None
    _default_service_provider = ""
