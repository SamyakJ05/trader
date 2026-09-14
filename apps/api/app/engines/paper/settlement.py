"""Delivery inventory. All mutations share the cash ledger's account lock."""

from app.core.logging import get_logger


logger = get_logger(__name__)


class SettlementError(RuntimeError):
    """Raised when settlement would violate an accounting invariant."""


from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import PaperHolding, PendingSettlement, Position
from app.domain.calendar import IST
from app.engines.paper.ledger import lock_account


async def get_holding(db, account_id, symbol, exchange):
    return (
        await db.execute(
            select(PaperHolding)
            .where(
                PaperHolding.broker_account_id == account_id,
                PaperHolding.symbol == symbol,
                PaperHolding.exchange == exchange,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def settle_account(db, account_id, now=None):
    now = now or datetime.now(timezone.utc)
    await lock_account(db, account_id)
    rows = (
        (
            await db.execute(
                select(PendingSettlement)
                .where(
                    PendingSettlement.broker_account_id == account_id,
                    PendingSettlement.settles_on <= now.astimezone(IST).date(),
                    PendingSettlement.settled_at.is_(None),
                    PendingSettlement.cancelled_at.is_(None),
                )
                .order_by(PendingSettlement.settles_on, PendingSettlement.id)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        holding = await get_holding(db, account_id, row.symbol, row.exchange)
        if holding is None:
            holding = PaperHolding(
                broker_account_id=account_id,
                symbol=row.symbol,
                exchange=row.exchange,
                quantity=0,
                average_price=0,
            )
            db.add(holding)
        total = holding.quantity + row.quantity
        holding.average_price = (
            holding.average_price * holding.quantity + row.price * row.quantity
        ) / total
        holding.quantity = total
        position = (
            await db.execute(
                select(Position).where(
                    Position.broker_account_id == account_id,
                    Position.symbol == row.symbol,
                    Position.exchange == row.exchange,
                    Position.product == "CNC",
                )
            )
        ).scalar_one_or_none()
        if position is not None:
            remaining = position.quantity - row.quantity
            if remaining < 0:
                # The position holds less than the amount being settled, which
                # should be impossible: reset_account cancels pending rows
                # before deleting positions. If it happens, the averaging below
                # would divide by a negative and write a plausible-looking but
                # wrong price, corrupting cost basis silently. Refuse instead.
                raise SettlementError(
                    f"Settlement of {row.quantity} {row.symbol} exceeds the "
                    f"CNC position of {position.quantity}; refusing to write a "
                    "negative position rather than corrupt its cost basis"
                )
            position.average_price = (
                (position.average_price * position.quantity - row.price * row.quantity) / remaining
                if remaining
                else 0
            )
            position.quantity = remaining
        row.settled_at = now
        await db.flush()
    return len(rows)


async def settle_due(db, now=None):
    now = now or datetime.now(timezone.utc)
    accounts = (
        (
            await db.execute(
                select(PendingSettlement.broker_account_id)
                .where(
                    PendingSettlement.settles_on <= now.astimezone(IST).date(),
                    PendingSettlement.settled_at.is_(None),
                    PendingSettlement.cancelled_at.is_(None),
                )
                .distinct()
                .order_by(PendingSettlement.broker_account_id)
            )
        )
        .scalars()
        .all()
    )
    settled, failed = 0, 0
    for account_id in accounts:
        # Isolate per account. This runs on the shared platform tick, so an
        # invariant violation on one account must not stop every other user's
        # settlements -- and a rollback must not discard the accounts that
        # already succeeded, hence the commit per account.
        try:
            await settle_account(db, account_id, now)
            await db.commit()
            settled += 1
        except Exception:
            await db.rollback()
            failed += 1
            logger.exception("settlement_failed", broker_account_id=str(account_id))
    if failed:
        logger.warning("settlement_partial", settled=settled, failed=failed)
    return settled
