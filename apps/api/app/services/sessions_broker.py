"""Broker session expiry.

Kite access tokens die at the daily exchange flush, roughly 6am IST, regardless
of when they were issued. An account still marked CONNECTED after that is
lying: every call through it will fail, strategies will throw on a schedule,
and the user has no idea they need to log in again.

This marks such accounts SESSION_EXPIRED so the UI can say so plainly, and
audits each transition. It never touches paper accounts, which have no session
to lose.
"""

from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import BrokerAccount
from app.domain.calendar import IST
from app.domain.enums import AuditEventType, Broker, BrokerAccountStatus
from app.services import audit

logger = get_logger(__name__)

# Kite invalidates every access token at the daily flush; the documented time
# is around 06:00 IST. Treated as a wall-clock event rather than a per-token
# lifetime because that is how the broker actually behaves — a token issued at
# 05:55 dies five minutes later.
KITE_FLUSH = time(6, 0)

# Brokers whose sessions expire at a daily flush rather than on their own
# schedule. Paper has no session at all.
_DAILY_FLUSH_BROKERS = {Broker.ZERODHA.value}


def last_flush(now: datetime | None = None) -> datetime:
    """The most recent daily flush at or before `now`, in UTC."""
    moment = (now or datetime.now(timezone.utc)).astimezone(IST)
    flush_today = moment.replace(
        hour=KITE_FLUSH.hour, minute=KITE_FLUSH.minute, second=0, microsecond=0
    )
    if moment < flush_today:
        flush_today -= timedelta(days=1)
    return flush_today.astimezone(timezone.utc)


def is_session_stale(account: BrokerAccount, now: datetime | None = None) -> bool:
    """Whether this account's broker session can no longer be valid.

    A session is stale once a flush has happened since it was stored. An
    explicit `session_expires_at` in the past also counts — some brokers tell
    us directly.
    """
    if account.broker not in _DAILY_FLUSH_BROKERS:
        return False
    if not account.session_token_enc:
        return False

    moment = now or datetime.now(timezone.utc)
    expires_at = account.session_expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= moment:
            return True

    # No explicit expiry: fall back to the flush. Without a stored expiry we
    # cannot tell when the token was issued, so we treat it as stale after the
    # most recent flush and let a reconnect prove otherwise.
    return expires_at is None and moment >= last_flush(moment)


async def expire_stale_sessions(db: AsyncSession, now: datetime | None = None) -> int:
    """Mark accounts whose broker session has been flushed.

    Runs on a schedule rather than on demand so the brokers page is honest
    before the user tries to trade, not after their first failed order.
    """
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.broker.in_(_DAILY_FLUSH_BROKERS),
            BrokerAccount.status == BrokerAccountStatus.CONNECTED.value,
        )
    )
    expired = 0
    for account in result.scalars():
        if not is_session_stale(account, now):
            continue
        # Isolate per account: one failure must not stop the rest, on the same
        # reasoning as the paper tick.
        try:
            account.status = BrokerAccountStatus.SESSION_EXPIRED.value
            account.status_message = (
                "Daily broker session expired — reconnect to continue trading"
            )
            await audit.emit(
                db,
                AuditEventType.BROKER_SESSION,
                user_id=account.user_id,
                entity_type="broker_account",
                entity_id=account.id,
                payload={"action": "session_expired", "reason": "daily_flush"},
            )
            await db.commit()
            expired += 1
        except Exception:
            await db.rollback()
            logger.exception("session_expiry_failed", broker_account_id=str(account.id))
    if expired:
        logger.info("broker_sessions_expired", count=expired)
    return expired
