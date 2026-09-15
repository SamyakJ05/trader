"""Paper trading engine.

Fills open paper orders against the simulated price feed, maintains
positions, realized/unrealized P&L and virtual cash. Runs from the worker
tick; also invoked inline for immediate market-order feedback.

Fill model:
- MARKET / triggered SL_M: fill at sim price with slippage (5 bps against you)
- LIMIT: fills when sim price crosses the limit; never worse than limit
- SL / SL_M: arm when trigger crossed, then behave as LIMIT / MARKET
- each tick fills the full remainder with PARTIAL_FILL_P probability of
  filling only half — exercises the partial-fill path downstream code
  must handle with real brokers
"""

import random
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BrokerAccount, Fill, Order, Position, PaperHolding, PendingSettlement
from app.domain.enums import (
    AuditEventType,
    Broker,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
)
from app.engines.paper import market_sim, ledger, settlement
from app.engines.paper.ledger import get_cash
from app.domain.calendar import IST, settlement_date, CalendarUnavailable
from app.engines.paper.charges import compute_charges
from app.engines.paper.pnl import apply_fill
from app.core.logging import get_logger
from app.services import audit, daily_pnl

logger = get_logger(__name__)

SLIPPAGE_BPS = Decimal("5")
PARTIAL_FILL_P = 0.3
INITIAL_PAPER_CASH = Decimal("1000000.00")

_WORKING = [
    OrderStatus.ACCEPTED.value,
    OrderStatus.OPEN.value,
    OrderStatus.PARTIALLY_FILLED.value,
]


def _slip(price: Decimal, side: str) -> Decimal:
    factor = SLIPPAGE_BPS / Decimal(10000)
    slipped = price * (1 + factor) if side == OrderSide.BUY.value else price * (1 - factor)
    return slipped.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _fill_price(order: Order, market_price: Decimal) -> Decimal | None:
    """None = not fillable at this price."""
    side, otype = order.side, order.order_type
    limit = order.price
    trigger = order.trigger_price

    if otype in (OrderType.SL.value, OrderType.SL_M.value):
        armed = market_price >= trigger if side == OrderSide.BUY.value else market_price <= trigger
        if not armed:
            return None
        if otype == OrderType.SL_M.value:
            return _slip(market_price, side)
        otype = OrderType.LIMIT.value  # armed SL behaves as limit

    if otype == OrderType.MARKET.value:
        return _slip(market_price, side)

    if otype == OrderType.LIMIT.value:
        if side == OrderSide.BUY.value and market_price <= limit:
            return min(market_price, limit)
        if side == OrderSide.SELL.value and market_price >= limit:
            return max(market_price, limit)
    return None


async def _apply_fill_to_position(
    db: AsyncSession, order: Order, fill_qty: int, fill_price: Decimal
) -> tuple[Position, Decimal]:
    """Returns the position and the realized-P&L delta this fill produced."""
    result = await db.execute(
        select(Position)
        .where(
            Position.broker_account_id == order.broker_account_id,
            Position.symbol == order.symbol,
            Position.exchange == order.exchange,
            Position.product == order.product,
        )
        .execution_options(populate_existing=True)
    )
    position = result.scalar_one_or_none()
    if position is None:
        position = Position(
            user_id=order.user_id,
            broker_account_id=order.broker_account_id,
            environment=order.environment,
            symbol=order.symbol,
            exchange=order.exchange,
            product=order.product,
        )
        db.add(position)

    signed = fill_qty if order.side == OrderSide.BUY.value else -fill_qty
    realized_before = position.realized_pnl or Decimal("0")
    position.quantity, position.average_price, position.realized_pnl = apply_fill(
        position.quantity or 0,
        position.average_price or Decimal("0"),
        realized_before,
        signed,
        fill_price,
    )
    position.last_price = fill_price
    return position, position.realized_pnl - realized_before


async def _first_delivery_sell_of_day(db: AsyncSession, order: Order) -> bool:
    """Whether this is the day's first delivery sell of this scrip.

    The DP charge is levied per scrip per day on the demat debit, not per
    trade: selling the same holding twice in one session pays it once.
    """
    if order.product != ProductType.CNC.value or order.side != OrderSide.SELL.value:
        return False
    day_start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(Fill)
        .join(Order, Fill.order_id == Order.id)
        .where(
            Order.broker_account_id == order.broker_account_id,
            Order.symbol == order.symbol,
            Order.product == ProductType.CNC.value,
            Order.side == OrderSide.SELL.value,
            Fill.ts >= day_start,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is None


async def _execute_fill(
    db: AsyncSession, redis: aioredis.Redis, order: Order, fill_qty: int, fill_price: Decimal
) -> bool:
    account = await db.get(BrokerAccount, order.broker_account_id)
    breakdown = compute_charges(
        broker=account.broker if account else Broker.PAPER,
        side=OrderSide(order.side),
        product=ProductType(order.product),
        quantity=fill_qty,
        price=fill_price,
        exchange=order.exchange,
        is_first_sell_of_scrip_today=await _first_delivery_sell_of_day(db, order),
    )
    charges = breakdown.total
    cash = await get_cash(db, order.broker_account_id)
    holding = None
    due = None
    reason = None
    if order.side == "BUY" and cash < ledger.money(fill_price * fill_qty) + charges:
        reason = "Insufficient paper cash including charges"
    if order.product == "CNC":
        if order.side == "BUY":
            try:
                due = settlement_date(datetime.now(IST).date())
            except CalendarUnavailable as exc:
                reason = str(exc)
        else:
            holding = await settlement.get_holding(
                db, order.broker_account_id, order.symbol, order.exchange
            )
            if holding is None or holding.quantity < fill_qty:
                reason = "Insufficient settled holdings; unsettled delivery cannot be sold"
    if reason:
        old = order.status
        order.status = OrderStatus.REJECTED.value
        order.status_message = reason
        await audit.emit(
            db,
            AuditEventType.ORDER_STATE_CHANGED,
            user_id=order.user_id,
            entity_type="order",
            entity_id=order.id,
            payload={"from": old, "to": order.status, "reason": reason},
        )
        return False
    fill = Fill(
        id=uuid.uuid4(), order_id=order.id, quantity=fill_qty, price=fill_price, charges=charges
    )
    db.add(fill)
    await db.flush()

    prev_filled = order.filled_quantity or 0
    prev_avg = order.average_fill_price or Decimal("0")
    new_filled = prev_filled + fill_qty
    order.average_fill_price = (
        (prev_avg * prev_filled + fill_price * fill_qty) / new_filled
    ).quantize(Decimal("0.0001"))
    order.filled_quantity = new_filled
    old_status = order.status
    order.status = (
        OrderStatus.FILLED.value
        if new_filled >= order.quantity
        else OrderStatus.PARTIALLY_FILLED.value
    )

    if holding is not None:
        realized_delta = (fill_price - holding.average_price) * fill_qty
        # Preserve cumulative realized P&L after inventory leaves positions.
        position, _ = await _apply_fill_to_position(db, order, 0, fill_price)
        position.realized_pnl += realized_delta
        holding.quantity -= fill_qty
        if holding.quantity == 0:
            holding.average_price = Decimal("0")
    else:
        position, realized_delta = await _apply_fill_to_position(db, order, fill_qty, fill_price)
    position.realized_pnl -= charges
    if due is not None:
        db.add(
            PendingSettlement(
                broker_account_id=order.broker_account_id,
                fill_id=fill.id,
                symbol=order.symbol,
                exchange=order.exchange,
                quantity=fill_qty,
                price=fill_price,
                settles_on=due,
            )
        )
    # Feed the MAX_DAILY_LOSS rule: per-day realized counter in Redis.
    await daily_pnl.add_realized(
        redis, order.user_id, order.environment, realized_delta - charges, db
    )

    notional = fill_price * fill_qty
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
            "qty": fill_qty,
            "price": fill_price,
            "charges": charges,
            # Itemised so the audit trail explains a cost rather than
            # asserting one.
            "charge_breakdown": breakdown.as_dict(),
        },
    )
    await audit.emit(
        db,
        AuditEventType.ORDER_STATE_CHANGED,
        user_id=order.user_id,
        entity_type="order",
        entity_id=order.id,
        correlation_id=order.client_order_id,
        payload={"from": old_status, "to": order.status},
    )

    return True


async def try_fill_order(db: AsyncSession, redis: aioredis.Redis, order: Order) -> bool:
    """Attempt one fill pass for a single order. Returns True if filled (any amount)."""
    await ledger.lock_account(db, order.broker_account_id)
    await db.refresh(order)
    if order.status not in _WORKING:
        return False
    await settlement.settle_account(db, order.broker_account_id)
    if order.status == OrderStatus.ACCEPTED.value:
        old = order.status
        order.status = OrderStatus.OPEN.value
        await audit.emit(
            db,
            AuditEventType.ORDER_STATE_CHANGED,
            user_id=order.user_id,
            entity_type="order",
            entity_id=order.id,
            correlation_id=order.client_order_id,
            payload={"from": old, "to": order.status},
        )

    market_price = await market_sim.get_price(redis, order.symbol)
    fill_price = _fill_price(order, market_price)
    if fill_price is None:
        return False

    remaining = order.quantity - (order.filled_quantity or 0)
    if remaining <= 0:
        return False
    fill_qty = remaining
    if remaining > 1 and random.random() < PARTIAL_FILL_P:
        fill_qty = max(1, remaining // 2)

    return await _execute_fill(db, redis, order, fill_qty, fill_price)


async def process_open_orders(db: AsyncSession, redis: aioredis.Redis) -> int:
    """Worker tick: advance fills for every working paper order."""
    result = await db.execute(
        select(Order)
        .where(Order.environment == "paper", Order.status.in_(_WORKING))
        .order_by(Order.broker_account_id, Order.id)
    )
    orders = result.scalars().all()
    filled, failed = 0, 0
    for order in orders:
        # Isolate per order. This loop covers every user's working orders on a
        # shared tick, so one order that cannot be filled -- a settlement
        # invariant, a calendar gap, a data problem -- must not stop the rest
        # of the platform from trading. Committing per order also keeps a
        # rollback from discarding fills that already succeeded.
        try:
            if await try_fill_order(db, redis, order):
                filled += 1
            await db.commit()
        except Exception:
            await db.rollback()
            failed += 1
            logger.exception(
                "order_fill_failed",
                order_id=str(order.id),
                broker_account_id=str(order.broker_account_id),
            )
    if failed:
        logger.warning("paper_fills_partial", filled=filled, failed=failed)
    return filled


async def mark_positions(db: AsyncSession, redis: aioredis.Redis) -> None:
    """Mark open paper positions to the latest simulated price."""
    result = await db.execute(
        select(Position).where(Position.environment == "paper", Position.quantity != 0)
    )
    for position in result.scalars():
        position.last_price = await market_sim.get_price(redis, position.symbol)


async def cancel_order(db: AsyncSession, order: Order) -> None:
    await ledger.lock_account(db, order.broker_account_id)
    await db.refresh(order)
    if OrderStatus(order.status).is_terminal:
        raise ValueError(f"Order already terminal: {order.status}")
    old = order.status
    order.status = OrderStatus.CANCELLED.value
    await audit.emit(
        db,
        AuditEventType.ORDER_STATE_CHANGED,
        user_id=order.user_id,
        entity_type="order",
        entity_id=order.id,
        correlation_id=order.client_order_id,
        payload={"from": old, "to": order.status, "via": "cancel"},
    )


async def reset_account(db: AsyncSession, account: BrokerAccount) -> None:
    """Wipe paper positions and restore initial cash. Order/fill/audit history
    is kept, but working orders are cancelled first — otherwise they would
    keep filling into the freshly reset account."""
    await ledger.lock_account(db, account.id)
    result = await db.execute(
        select(Order).where(Order.broker_account_id == account.id, Order.status.in_(_WORKING))
    )
    for order in result.scalars():
        await cancel_order(db, order)

    result = await db.execute(select(Position).where(Position.broker_account_id == account.id))
    for position in result.scalars():
        await db.delete(position)
    for holding in (
        await db.execute(select(PaperHolding).where(PaperHolding.broker_account_id == account.id))
    ).scalars():
        await db.delete(holding)
    for pending in (
        await db.execute(
            select(PendingSettlement).where(
                PendingSettlement.broker_account_id == account.id,
                PendingSettlement.settled_at.is_(None),
                PendingSettlement.cancelled_at.is_(None),
            )
        )
    ).scalars():
        pending.cancelled_at = datetime.now(timezone.utc)
    await ledger.reset(db, account.id)
