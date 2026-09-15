"""Live fill accounting.

The paper engine books its own fills: it creates the Fill row, updates the
position, moves cash and feeds the daily-loss counter. Live orders have none of
that — the broker fills them, and we only learn about it from a postback or a
poll.

Without this, a live fill updates the order row and nothing else. Positions
would not move, the cash ledger would never budge, and the realized-P&L
counter that MAX_DAILY_LOSS reads would stay at zero however much the account
lost, silently disabling the one control meant to stop a bad day. That is the
whole reason this module exists.

Everything here runs under the account lock, for the same reason the paper
engine takes it: booking a fill is a read-modify-write of the position and of
the ledger's running balance, and broker postbacks arrive concurrently.

Charges are the broker's actual figures where the postback supplies them, and
our own estimate where it does not — flagged either way, because an estimated
charge is a prediction and a booked one is a fact.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import BrokerAccount, Fill, Order
from app.domain.calendar import IST
from app.domain.enums import AuditEventType, Environment, OrderSide, ProductType
from app.engines.paper import ledger
from app.engines.paper.charges import compute_charges
from app.engines.paper.engine import _apply_fill_to_position
from app.services import audit, daily_pnl

logger = get_logger(__name__)


async def _already_booked(db: AsyncSession, order: Order) -> int:
    """How much of this order we have already accounted for.

    Postbacks repeat and report cumulative quantities, so booking the reported
    figure blindly would double-count a position every time Kite resends.
    """
    result = await db.execute(select(Fill.quantity).where(Fill.order_id == order.id))
    return sum(result.scalars())


async def book_fill(
    db: AsyncSession,
    redis,
    *,
    account: BrokerAccount,
    order: Order,
    filled_quantity: int,
    average_price: Decimal,
    charges: Decimal | None = None,
) -> Fill | None:
    """Record the part of a live fill we have not booked yet.

    `filled_quantity` is cumulative, as brokers report it. Returns the Fill
    created, or None when there is nothing new.
    """
    if account.environment != Environment.LIVE.value:
        # Paper books its own fills inside the simulator; routing them through
        # here as well would double-count everything.
        return None
    if filled_quantity <= 0 or average_price <= 0:
        return None

    # Serialize on the account before reading what is already booked. Brokers
    # resend postbacks aggressively and report CUMULATIVE quantities, so two
    # deliveries of the same fill race here: both read the same `booked`, both
    # compute the same positive delta, and both book it. The dedupe below is a
    # read-then-write and cannot defend itself without this -- it only narrows
    # the window. The same lock protects _apply_fill_to_position, which is
    # itself a read-modify-write of the position row.
    await ledger.lock_account(db, order.broker_account_id)

    booked = await _already_booked(db, order)
    delta = filled_quantity - booked
    if delta <= 0:
        return None
    if delta < 0:  # pragma: no cover - defensive
        logger.warning("live_fill_went_backwards", order_id=str(order.id))
        return None

    estimated = charges is None
    if estimated:
        # The broker's own figure is authoritative when we get it. Ours is a
        # prediction from the same rate table the paper engine uses, and is
        # marked as such so a reconciliation against the contract note can
        # tell the two apart.
        charges = compute_charges(
            broker=account.broker,
            side=OrderSide(order.side),
            product=ProductType(order.product),
            quantity=delta,
            price=average_price,
            exchange=order.exchange,
            on=order.placed_at.astimezone(IST).date() if order.placed_at else None,
        ).total

    fill = Fill(
        order_id=order.id,
        quantity=delta,
        price=average_price,
        charges=charges,
        broker_fill_id=None,
    )
    db.add(fill)
    await db.flush()

    _, realized_delta = await _apply_fill_to_position(db, order, delta, average_price)

    # The counter MAX_DAILY_LOSS reads. Without this a live account could lose
    # any amount and the limit would never fire.
    await daily_pnl.add_realized(
        redis, order.user_id, order.environment, realized_delta - charges, db
    )

    # Move the cash ledger, exactly as the paper engine does for its own fills.
    # Without this a live account's ledger never moves: sum(CashLedger.amount)
    # drifts from the fills table permanently, and nothing detects it, because
    # the ledger is the only place a balance is derived from. The broker's own
    # funds figure is a separate snapshot and does not reconcile this.
    notional = average_price * delta
    await ledger.append(
        db,
        order.broker_account_id,
        order.side,
        -notional if order.side == "BUY" else notional,
        fill_id=fill.id,
    )
    await ledger.append(db, order.broker_account_id, "CHARGES", -charges, fill_id=fill.id)

    await audit.emit(
        db,
        AuditEventType.ORDER_FILL,
        user_id=order.user_id,
        entity_type="order",
        entity_id=order.id,
        correlation_id=order.client_order_id,
        payload={
            "qty": delta,
            "price": str(average_price),
            "charges": str(charges),
            "charges_estimated": estimated,
            "cumulative_filled": filled_quantity,
            "via": "live_reconciliation",
        },
    )
    logger.info(
        "live_fill_booked",
        order_id=str(order.id),
        quantity=delta,
        estimated_charges=estimated,
    )
    return fill


async def book_from_payload(
    db: AsyncSession, redis, *, account: BrokerAccount, order: Order, payload: dict
) -> Fill | None:
    """Book a fill described by a broker postback."""
    try:
        filled_quantity = int(payload.get("filled_quantity") or 0)
    except (TypeError, ValueError):
        return None
    average = payload.get("average_price")
    if average is None:
        return None
    try:
        average_price = Decimal(str(average))
    except ArithmeticError:
        return None
    if not average_price.is_finite() or average_price <= 0:
        return None

    # Kite does not itemise charges on a postback, so ours are an estimate
    # until the contract note arrives.
    return await book_fill(
        db,
        redis,
        account=account,
        order=order,
        filled_quantity=filled_quantity,
        average_price=average_price,
    )
