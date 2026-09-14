"""Outbound email.

Two backends behind one protocol: Resend in production, a console logger in
development. The console default is deliberate — every flow that sends mail
(invites, password reset) is then testable end to end before a sending domain
is verified, with the link printed to the log instead of delivered.

Sending is best-effort. A failed send must never fail the request that
triggered it: an invite whose email bounced is still a valid invite row the
operator can re-send, whereas a 500 would leave them unsure whether it existed.
"""

from typing import Protocol

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"


class EmailNotConfigured(RuntimeError):
    """Raised when no provider is configured outside debug."""


class EmailMessage:
    def __init__(self, to: str, subject: str, body: str):
        self.to = to
        self.subject = subject
        self.body = body


class EmailSender(Protocol):
    async def send(self, message: EmailMessage) -> bool:
        """Returns True if the message was accepted for delivery."""
        ...


class ConsoleSender:
    """Development backend: writes the message to the log so invite and reset
    links are usable without a mail provider."""

    async def send(self, message: EmailMessage) -> bool:
        logger.info(
            "email_console",
            to=message.to,
            subject=message.subject,
            body=message.body,
        )
        return True


class ResendSender:
    def __init__(self, api_key: str, sender: str):
        self._api_key = api_key
        self._sender = sender

    async def send(self, message: EmailMessage) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    RESEND_ENDPOINT,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "from": self._sender,
                        "to": [message.to],
                        "subject": message.subject,
                        "text": message.body,
                    },
                )
            if response.status_code >= 400:
                # Body may carry a provider reason (unverified domain, bad
                # recipient); log it, but never the API key.
                logger.warning(
                    "email_send_failed",
                    to=message.to,
                    status=response.status_code,
                    detail=response.text[:500],
                )
                return False
            return True
        except Exception:
            logger.exception("email_send_error", to=message.to)
            return False


def get_sender() -> EmailSender:
    """Resend when configured; the console backend only in debug.

    The console backend writes invite links — raw, single-use tokens — into the
    application log. That is right for development and wrong everywhere else,
    so a missing key outside debug raises rather than silently downgrading:
    a misconfigured production deploy should fail loudly, not quietly publish
    account-creation tokens to its logs.
    """
    settings = get_settings()
    if settings.resend_api_key and settings.email_from:
        return ResendSender(settings.resend_api_key, settings.email_from)
    if settings.debug:
        return ConsoleSender()
    raise EmailNotConfigured(
        "Email is not configured: set RESEND_API_KEY and EMAIL_FROM, "
        "or run with DEBUG=true to log messages to the console instead."
    )


async def send(to: str, subject: str, body: str) -> bool:
    """Best-effort send. Returns False rather than raising, so a delivery
    problem never fails the request that triggered it — callers surface the
    failure to the operator instead (an invite, for example, stays valid and
    its link can be passed on by hand)."""
    try:
        sender = get_sender()
    except EmailNotConfigured:
        logger.error("email_not_configured", to=to, subject=subject)
        return False
    try:
        return await sender.send(EmailMessage(to=to, subject=subject, body=body))
    except Exception:
        logger.exception("email_send_error", to=to)
        return False
