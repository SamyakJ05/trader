"""Live order reconciliation by polling.

Some brokers push order state to a webhook; ICICI Breeze does not. Its
capability matrix says `order_postbacks=False`, and that is accurate -- the
Breeze tick stream's docstring notes that order state "comes from polling".
That polling did not exist. `BrokerAdapter.get_orders()` had no non-test
caller at all.

The consequence is the failure `live_fills` was written to prevent, arrived at
by a different road: a live order that filled at the broker updated nothing
here. The order row kept whatever status it was given at placement, no Fill
was created, the position never moved, the cash ledger never budged, and the
realized-P&L counter that MAX_DAILY_LOSS reads stayed at zero however much the
account lost -- silently disabling the one control meant to stop a bad day.

This closes that by asking the broker, on a timer, what actually happened to
every order we believe is still open.

Design notes:

* Booking goes through live_fills.book_fill, which is idempotent: it reads how
  much of an order is already booked and writes only the delta, under the
  account lock. So polling the same fill repeatedly -- which is the normal
  case, since an order stays FILLED at the broker all day -- books it once.
  That property is what makes a poller safe to run beside a webhook, if a
  broker ever gains one.

* Only orders we believe are live are examined. A terminal order is not
  re-checked: it cannot change at the broker, and re-reading it would spend
  rate budget to learn nothing.

* One account's failure must not stop the others, and one order's must not
  stop its account's. A broker outage should cost a cycle, not the loop.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import BrokerError, SessionExpiredError
from app.adapters.registry import get_adapter
from app.core.logging import get_logger
from app.db.models import BrokerAccount, Order
from app.domain.enums import Broker, BrokerAccountStatus, Environment, OrderStatus
from app.services import live_fills

logger = get_logger(__name__)

# Brokers whose order state must be polled because they do not push it.
# Zerodha is absent deliberately: it posts back, and postbacks.py already
# books those fills. Polling it as well would be harmless (book_fill dedupes)
# but would spend rate budget for no new information.
POLLED_BROKERS = {Broker.ICICI_BREEZE.value}

# Statuses we consider still live at the broker, and therefore worth asking
# about. PENDING_RISK and REJECTED_RISK never reached the broker at all.
_OPEN_STATUSES = (
    OrderStatus.SUBMITTED.value,
    OrderStatus.OPEN.value,
    OrderStatus.PARTIALLY_FILLED.value,
)


async def open_live_orders(db: AsyncSession, account: BrokerAccount) -> list[Order]:
    """Orders on this account we believe are still working at the broker."""
    result = await db.execute(
        select(Order).where(
            Order.broker_account_id == account.id,
            Order.environment == Environment.LIVE.value,
            Order.status.in_(_OPEN_STATUSES),
            Order.broker_order_id.is_not(None),
        )
    )
    return list(result.scalars())


async def reconcile_account(db: AsyncSession, redis, account: BrokerAccount) -> int:
    """Bring one account's open orders in line with the broker. Returns the
    number of orders whose state changed."""
    orders = await open_live_orders(db, account)
    if not orders:
        return 0

    adapter = get_adapter(account)
    broker_orders = {o.broker_order_id: o for o in await adapter.get_orders()}

    changed = 0
    for order in orders:
        remote = broker_orders.get(order.broker_order_id)
        if remote is None:
            # The broker does not know this order. Not treated as cancelled:
            # Breeze's order list is scoped to a single day, so an order
            # placed yesterday is absent from today's list without having
            # gone anywhere. Guessing "cancelled" here would zero out a
            # position the account actually holds.
            logger.info(
                "reconcile_order_absent",
                order_id=str(order.id),
                broker_order_id=order.broker_order_id,
            )
            continue
        try:
            if await _apply(db, redis, account, order, remote):
                changed += 1
        except Exception:
            # One order's accounting failure must not abandon the rest of the
            # account's orders, several of which may be fills that still need
            # booking.
            await db.rollback()
            logger.exception("reconcile_order_failed", order_id=str(order.id))
    return changed


async def _apply(db: AsyncSession, redis, account, order: Order, remote) -> bool:
    """Apply one broker order's state to ours. Returns whether anything moved."""
    moved = False

    # Book the fill first, then record status. If the process dies between the
    # two, the next cycle re-reads an order still marked open and books the
    # remainder -- whereas marking it FILLED first would exclude it from the
    # next query and strand the accounting permanently.
    filled = int(remote.filled_quantity or 0)
    price = remote.average_fill_price
    if filled > 0 and price and Decimal(str(price)) > 0:
        fill = await live_fills.book_fill(
            db,
            redis,
            account=account,
            order=order,
            filled_quantity=filled,
            average_price=Decimal(str(price)),
        )
        if fill is not None:
            moved = True
            logger.info(
                "reconcile_booked_fill",
                order_id=str(order.id),
                quantity=fill.quantity,
                price=str(fill.price),
            )

    status = remote.status.value if hasattr(remote.status, "value") else str(remote.status)
    if status != order.status:
        order.status = status
        order.status_message = remote.status_message
        moved = True
    if filled != order.filled_quantity:
        order.filled_quantity = filled
        moved = True
    if price is not None:
        order.average_fill_price = Decimal(str(price))

    if moved:
        await db.commit()
    return moved


async def reconcile_all(db_factory, redis) -> int:
    """Reconcile every connected live account on a polled broker.

    Takes a session factory rather than a session: each account is committed
    on its own, so one account's failure cannot roll back another's booked
    fills.
    """
    async with db_factory() as db:
        result = await db.execute(
            select(BrokerAccount).where(
                BrokerAccount.broker.in_(POLLED_BROKERS),
                BrokerAccount.environment == Environment.LIVE.value,
                BrokerAccount.status == BrokerAccountStatus.CONNECTED.value,
            )
        )
        accounts = list(result.scalars())

    total = 0
    for account in accounts:
        async with db_factory() as db:
            try:
                total += await reconcile_account(db, redis, account)
            except SessionExpiredError:
                # Expected daily. The session job marks the account; there is
                # nothing to do here but skip it.
                logger.info("reconcile_session_expired", broker_account_id=str(account.id))
            except BrokerError:
                await db.rollback()
                logger.warning(
                    "reconcile_broker_error", broker_account_id=str(account.id)
                )
            except Exception:
                await db.rollback()
                logger.exception("reconcile_failed", broker_account_id=str(account.id))
    return total
