"""Broker session expiry.

Broker sessions die daily, and brokers disagree about when. Kite tokens go at
the exchange flush around 6am IST; Breeze sessions go at midnight IST or 24
hours from issue, whichever is first. Neither can be refreshed
programmatically — both need the user back at a browser login.

An account still marked CONNECTED past its broker's flush is lying: every call
through it will fail, strategies will throw on a schedule, and the user has no
idea a re-login is what is needed. Using one broker's flush time for another
is the same lie with a different duration.

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
from app.domain.enums import AuditEventType, Broker, BrokerAccountStatus, StrategyStatus
from app.services import audit

logger = get_logger(__name__)

# Kite invalidates every access token at the daily flush; the documented time
# is around 06:00 IST. Treated as a wall-clock event rather than a per-token
# lifetime because that is how the broker actually behaves — a token issued at
# 05:55 dies five minutes later.
KITE_FLUSH = time(6, 0)

# Breeze sessions die at midnight IST or 24 hours from issue, whichever comes
# first — ICICI cite SEBI guidance for the daily reset. Using Kite's 06:00
# would leave a Breeze account claiming to be connected for six hours after
# its session was already dead.
BREEZE_FLUSH = time(0, 0)

# When each broker's sessions die. Paper has no session at all, so it is
# absent rather than mapped to anything.
_DAILY_FLUSH: dict[str, time] = {
    Broker.ZERODHA.value: KITE_FLUSH,
    Broker.ICICI_BREEZE.value: BREEZE_FLUSH,
}

_DAILY_FLUSH_BROKERS = set(_DAILY_FLUSH)


def last_flush(now: datetime | None = None, *, broker: str | None = None) -> datetime:
    """The most recent daily flush at or before `now`, in UTC.

    `broker` selects the flush time; without one, Kite's is used, which is the
    later of the two and therefore the conservative choice for a caller that
    does not know.
    """
    flush_time = _DAILY_FLUSH.get(broker or "", KITE_FLUSH)
    moment = (now or datetime.now(timezone.utc)).astimezone(IST)
    flush_today = moment.replace(
        hour=flush_time.hour, minute=flush_time.minute, second=0, microsecond=0
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
    return expires_at is None and moment >= last_flush(moment, broker=account.broker)


async def _notify_expiry(db: AsyncSession, account: BrokerAccount) -> None:
    """Tell the owner their session died, if anything was depending on it.

    Breeze sessions die at midnight IST and ICICI publish no way to renew one
    programmatically -- a browser login is required daily. So an unattended
    live strategy stops at midnight and stays stopped until a person logs in,
    and without this the only signal is that orders quietly stop appearing.

    Sent only when a strategy is actually RUNNING on the account. A warning
    about an idle account is noise, and noise is how a real warning gets
    ignored.

    Best-effort: email failure must not undo the expiry, which is a fact about
    the broker regardless of whether anyone was told.
    """
    from app.db.models import Strategy, User
    from app.services import email

    try:
        running = (
            await db.execute(
                select(Strategy.name).where(
                    Strategy.broker_account_id == account.id,
                    Strategy.status == StrategyStatus.RUNNING.value,
                )
            )
        ).scalars().all()
        if not running:
            return

        user = await db.get(User, account.user_id)
        if user is None or not user.email:
            return

        names = ", ".join(running[:5])
        more = f" and {len(running) - 5} more" if len(running) > 5 else ""
        await email.send(
            user.email,
            f"Trading stopped: {account.label} session expired",
            (
                f"Your {account.broker} session expired at the daily reset, so "
                f"these strategies have stopped trading:\n\n  {names}{more}\n\n"
                "They are still RUNNING and will resume on their own once the "
                "account is reconnected — nothing needs restarting.\n\n"
                "Reconnecting needs a browser login: your broker publishes no "
                "way to renew a session automatically.\n\n"
                "Open the Brokers page to reconnect."
            ),
        )
        logger.info(
            "session_expiry_notified",
            broker_account_id=str(account.id),
            strategies=len(running),
        )
    except Exception:
        # Never let a notification failure affect the expiry itself.
        logger.exception("session_expiry_notify_failed", broker_account_id=str(account.id))


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
            await _notify_expiry(db, account)
        except Exception:
            await db.rollback()
            logger.exception("session_expiry_failed", broker_account_id=str(account.id))
    if expired:
        logger.info("broker_sessions_expired", count=expired)
    return expired
