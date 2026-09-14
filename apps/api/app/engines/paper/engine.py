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

from app.db.models import BrokerAccount, Fill, FundsSnapshot, Order, Position
from app.domain.enums import (
    AuditEventType,
    Broker,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
)
from app.engines.paper import market_sim
from app.engines.paper.charges import compute_charges
from app.engines.paper.pnl import apply_fill
from app.services import audit, daily_pnl

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
        armed = (
            market_price >= trigger if side == OrderSide.BUY.value else market_price <= trigger
        )
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


async def get_cash(db: AsyncSession, account_id: uuid.UUID) -> Decimal:
    result = await db.execute(
        select(FundsSnapshot)
        .where(FundsSnapshot.broker_account_id == account_id)
        .order_by(FundsSnapshot.ts.desc())
        .limit(1)
    )
    snap = result.scalar_one_or_none()
    return snap.available_cash if snap else INITIAL_PAPER_CASH


async def set_cash(db: AsyncSession, account_id: uuid.UUID, cash: Decimal) -> None:
    db.add(FundsSnapshot(broker_account_id=account_id, available_cash=cash, payload={}))


async def _apply_fill_to_position(
    db: AsyncSession, order: Order, fill_qty: int, fill_price: Decimal
) -> tuple[Position, Decimal]:
    """Returns the position and the realized-P&L delta this fill produced."""
    result = await db.execute(
        select(Position).where(
            Position.broker_account_id == order.broker_account_id,
            Position.symbol == order.symbol,
            Position.exchange == order.exchange,
            Position.product == order.product,
        )
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
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
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
) -> None:
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
    db.add(Fill(order_id=order.id, quantity=fill_qty, price=fill_price, charges=charges))

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

    _, realized_delta = await _apply_fill_to_position(db, order, fill_qty, fill_price)
    # Feed the MAX_DAILY_LOSS rule: per-day realized counter in Redis.
    await daily_pnl.add_realized(redis, order.user_id, order.environment, realized_delta)

    notional = fill_price * fill_qty
    cash = await get_cash(db, order.broker_account_id)
    cash = cash - notional - charges if order.side == OrderSide.BUY.value else cash + notional - charges
    await set_cash(db, order.broker_account_id, cash)

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


async def try_fill_order(db: AsyncSession, redis: aioredis.Redis, order: Order) -> bool:
    """Attempt one fill pass for a single order. Returns True if filled (any amount)."""
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

    await _execute_fill(db, redis, order, fill_qty, fill_price)
    return True


async def process_open_orders(db: AsyncSession, redis: aioredis.Redis) -> int:
    """Worker tick: advance fills for every working paper order."""
    result = await db.execute(
        select(Order).where(Order.environment == "paper", Order.status.in_(_WORKING))
    )
    orders = result.scalars().all()
    filled = 0
    for order in orders:
        if await try_fill_order(db, redis, order):
            filled += 1
    return filled


async def mark_positions(db: AsyncSession, redis: aioredis.Redis) -> None:
    """Mark open paper positions to the latest simulated price."""
    result = await db.execute(
        select(Position).where(Position.environment == "paper", Position.quantity != 0)
    )
    for position in result.scalars():
        position.last_price = await market_sim.get_price(redis, position.symbol)


async def cancel_order(db: AsyncSession, order: Order) -> None:
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
    result = await db.execute(
        select(Order).where(
            Order.broker_account_id == account.id, Order.status.in_(_WORKING)
        )
    )
    for order in result.scalars():
        await cancel_order(db, order)

    result = await db.execute(
        select(Position).where(Position.broker_account_id == account.id)
    )
    for position in result.scalars():
        await db.delete(position)
    await set_cash(db, account.id, INITIAL_PAPER_CASH)
