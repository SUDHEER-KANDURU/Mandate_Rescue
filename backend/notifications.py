"""Escalation notification abstraction (Phase 6.5).

Architecture:
    Recovery event
        → NotificationService
        → ProviderAdapter
        → delivery result

Providers:
    DemoAdapter    — default; marks all deliveries as DEMO/SIMULATION.
                     Never claims a real SMS/WhatsApp/email was sent.
    LogAdapter     — logs to the Python logger; useful for integration tests.

Adding a real provider later requires only implementing the ProviderAdapter
interface and setting NOTIFICATION_PROVIDER=<name> in the environment.  The
recovery engine never needs to change.

Design invariants:
  - Never claim a message was sent unless the provider confirms it.
  - Every delivery attempt is recorded with: channel, recipient_masked,
    status (DEMO | SENT | FAILED | SKIPPED), provider, and timestamp.
  - Recipient details are masked in logs (show only last 4 chars of phone).
  - No credentials are required for the demo adapter — it works out-of-the-box.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("mandate_rescue.notifications")

# ---------------------------------------------------------------------------
# Delivery result
# ---------------------------------------------------------------------------

class DeliveryStatus:
    SENT  = "SENT"    # provider confirmed delivery
    DEMO  = "DEMO"    # demo/simulation mode — no real message sent
    FAILED = "FAILED" # provider returned an error
    SKIPPED = "SKIPPED"  # not applicable (e.g. no phone number configured)


@dataclass
class DeliveryResult:
    status: str                        # DeliveryStatus value
    channel: str                       # "SMS" | "WhatsApp" | "Email" | "DEMO"
    provider: str                      # adapter name
    recipient_masked: str              # e.g. "****1234"
    message_preview: str               # first 100 chars of the message
    delivered_at: str = field(default_factory=lambda:
        datetime.now(timezone.utc).isoformat(timespec="seconds"))
    error: Optional[str] = None
    provider_message_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "status":              self.status,
            "channel":             self.channel,
            "provider":            self.provider,
            "recipient_masked":    self.recipient_masked,
            "message_preview":     self.message_preview,
            "delivered_at":        self.delivered_at,
            "error":               self.error,
            "provider_message_id": self.provider_message_id,
        }


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

class ProviderAdapter:
    """Base class for notification provider adapters."""

    name: str = "base"

    def send(self, channel: str, recipient: str, message: str) -> DeliveryResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Demo adapter (default — safe for local / CI use)
# ---------------------------------------------------------------------------

class DemoAdapter(ProviderAdapter):
    """Marks all deliveries as DEMO.  Never sends a real message.

    Used when no external provider is configured.  Every delivery is logged
    at INFO level so it is visible in the server log without polluting
    production channels.
    """
    name = "demo"

    def send(self, channel: str, recipient: str, message: str) -> DeliveryResult:
        masked = _mask_recipient(recipient)
        preview = message[:100]
        log.info("[DEMO] Would send %s to %s via %s: %s…", channel, masked, self.name, preview)
        return DeliveryResult(
            status=DeliveryStatus.DEMO,
            channel=channel,
            provider=self.name,
            recipient_masked=masked,
            message_preview=preview,
        )


# ---------------------------------------------------------------------------
# Log adapter (useful for integration tests)
# ---------------------------------------------------------------------------

class LogAdapter(ProviderAdapter):
    """Logs the message to the Python logger.  Marks delivery as DEMO."""
    name = "log"

    def send(self, channel: str, recipient: str, message: str) -> DeliveryResult:
        masked = _mask_recipient(recipient)
        log.info("[NOTIFICATION] channel=%s recipient=%s message=%r",
                 channel, masked, message[:200])
        return DeliveryResult(
            status=DeliveryStatus.DEMO,
            channel=channel,
            provider=self.name,
            recipient_masked=masked,
            message_preview=message[:100],
        )


# ---------------------------------------------------------------------------
# NotificationService
# ---------------------------------------------------------------------------

class NotificationService:
    """High-level notification dispatcher.

    Routes escalation events to the configured provider adapter.
    Returns a DeliveryResult — callers decide whether to persist it.
    """

    def __init__(self, adapter: ProviderAdapter = None):
        self._adapter = adapter or _get_default_adapter()

    @property
    def provider_name(self) -> str:
        return self._adapter.name

    def notify_escalation(self, case: dict, reason: str) -> DeliveryResult:
        """Send an escalation notification for a failed recovery case.

        Args:
            case:   mandate_failures dict (customer_id, amount, failure_reason).
            reason: human-readable escalation reason.

        Returns a DeliveryResult.  Never raises.
        """
        try:
            amount = int(round(float(case.get("amount") or 0)))
            cust_id = case.get("customer_id", "unknown")
            channel = "SMS"
            # The recipient is the customer_id itself for the demo adapter
            # (no real phone number is stored in synthetic data).
            recipient = cust_id
            message = (
                f"Escalation: Recovery failed for customer {cust_id}. "
                f"Amount: Rs {amount}. Reason: {reason}. "
                "Please contact the customer for manual follow-up."
            )
            result = self._adapter.send(channel, recipient, message)
            log.info(
                "Escalation notification customer_id=%s status=%s provider=%s",
                cust_id, result.status, result.provider,
            )
            return result
        except Exception as exc:
            log.error("Notification error for customer %s: %s",
                      case.get("customer_id"), exc, exc_info=True)
            return DeliveryResult(
                status=DeliveryStatus.FAILED,
                channel="SMS",
                provider=getattr(self._adapter, "name", "unknown"),
                recipient_masked="?",
                message_preview="",
                error=str(exc),
            )

    def notify_recovery(self, case: dict) -> DeliveryResult:
        """Send a recovery confirmation notification."""
        try:
            amount = int(round(float(case.get("amount") or 0)))
            cust_id = case.get("customer_id", "unknown")
            message = (
                f"Recovery confirmed for customer {cust_id}. "
                f"Amount Rs {amount} recovered successfully."
            )
            result = self._adapter.send("SMS", cust_id, message)
            return result
        except Exception as exc:
            log.error("Recovery notification error for customer %s: %s",
                      case.get("customer_id"), exc, exc_info=True)
            return DeliveryResult(
                status=DeliveryStatus.FAILED, channel="SMS",
                provider=getattr(self._adapter, "name", "unknown"),
                recipient_masked="?", message_preview="", error=str(exc),
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mask_recipient(recipient: str) -> str:
    """Mask all but the last 4 chars of a recipient identifier."""
    s = str(recipient or "")
    if len(s) <= 4:
        return "****"
    return "*" * (len(s) - 4) + s[-4:]


def _get_default_adapter() -> ProviderAdapter:
    """Return the adapter configured by NOTIFICATION_PROVIDER env var.

    Supported values: "demo" (default), "log".
    A real provider (e.g. "twilio", "aws_sns") would be added here when
    the integration is built; the rest of the codebase does not change.
    """
    provider = os.environ.get("NOTIFICATION_PROVIDER", "demo").lower()
    if provider == "log":
        return LogAdapter()
    # Default: safe demo adapter — never sends real messages.
    return DemoAdapter()


# Module-level singleton for normal use; tests create their own instances.
_default_service: Optional[NotificationService] = None


def get_notification_service() -> NotificationService:
    """Return the module-level NotificationService (creates it on first call)."""
    global _default_service
    if _default_service is None:
        _default_service = NotificationService()
    return _default_service
