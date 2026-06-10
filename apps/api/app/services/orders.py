"""Order pipeline — the single choke point for all order placement.

Sequence (no step skippable):
1. account ownership + environment check
2. idempotency (client_order_id unique per account; replays return the original)
3. audit ORDER_REQUESTED
4. risk engine evaluation (kill switches, limits, market hours)
5. dispatch: paper -> simulator; live -> triple gate then broker adapter
6. audit every transition
"""

import uuid
from decimal import Decimal

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import BrokerError, FeatureNotSupportedError
from app.adapters.registry import get_adapter
from app.core.config import get_settings
from app.db.models import BrokerAccount, IdempotencyKey, Order
from app.domain.capabilities import get_capabilities
from app.domain.enums import (
    AdapterStatus,
    AuditEventType,
    Broker,
    Environment,
    OrderStatus,
)
from app.domain.models import OrderRequest
from app.engines.paper import engine as paper_engine
from app.engines.paper import market_sim
from app.engines.risk.engine import RiskEngine
from app.services import audit


class OrderServiceError(Exception):
    pass


async def _existing_order(
    db: AsyncSession, account_id: uuid.UUID, client_order_id: str
) -> Order | None:
    result = await db.execute(
        select(Order).where(
            Order.broker_account_id == account_id,
            Order.client_order_id == client_order_id,
        )
    )
    return result.scalar_one_or_none()


def _live_gate(account: BrokerAccount) -> str | None:
    """Returns a refusal reason, or None if live dispatch is permitted."""
    if not get_settings().enable_live_trading:
        return "ENABLE_LIVE_TRADING is false (global gate)"
    if not account.live_enabled:
        return "live_enabled is false for this broker account"
    capabilities = get_capabilities(Broker(account.broker))
    if capabilities.adapter_status != AdapterStatus.WORKING:
        return (
            f"{account.broker} adapter status is '{capabilities.adapter_status}' — "
            "live orders require a verified adapter"
        )
    return None


async def place_order(
    db: AsyncSession,
    redis: aioredis.Redis,
    *,
    user_id: uuid.UUID,
    account: BrokerAccount,
    request: OrderRequest,
    client_order_id: str,
    strategy_id: uuid.UUID | None = None,
) -> Order:
    # Idempotent replay: same client_order_id returns the original order.
    existing = await _existing_order(db, account.id, client_order_id)
    if existing is not None:
        return existing

    try:
        return await _place_order_unchecked(
            db,
            redis,
            user_id=user_id,
            account=account,
            request=request,
            client_order_id=client_order_id,
            strategy_id=strategy_id,
        )
    except IntegrityError:
        # Concurrent request with the same client_order_id won the unique-index
        # race; the winner's row is the canonical order for this key.
        await db.rollback()
        existing = await _existing_order(db, account.id, client_order_id)
        if existing is not None:
            return existing
        raise


async def _place_order_unchecked(
    db: AsyncSession,
    redis: aioredis.Redis,
    *,
    user_id: uuid.UUID,
    account: BrokerAccount,
    request: OrderRequest,
    client_order_id: str,
    strategy_id: uuid.UUID | None = None,
) -> Order:
    db.add(
        IdempotencyKey(
            key=f"order:{account.id}:{client_order_id}",
            user_id=user_id,
            purpose="place_order",
        )
    )

    environment = account.environment
    await audit.emit(
        db,
        AuditEventType.ORDER_REQUESTED,
        user_id=user_id,
        entity_type="order_intent",
        entity_id=client_order_id,
        correlation_id=client_order_id,
        payload={"request": request.model_dump(), "account": str(account.id)},
    )

    # TODO(live-quotes): for LIVE accounts the notional/risk checks must use a
    # real broker quote, not the simulated feed. Blocked on a verified adapter
    # with market data; until then live dispatch is gated off anyway.
    last_price = await market_sim.get_price(redis, request.symbol)

    risk = RiskEngine(db, redis)
    result = await risk.evaluate(
        user_id=user_id,
        account=account,
        request=request,
        environment=environment,
        last_price=last_price,
        client_order_id=client_order_id,
        strategy_id=strategy_id,
    )

    order = Order(
        user_id=user_id,
        broker_account_id=account.id,
        strategy_id=strategy_id,
        environment=environment,
        client_order_id=client_order_id,
        symbol=request.symbol,
        exchange=request.exchange.value,
        side=request.side.value,
        order_type=request.order_type.value,
        product=request.product.value,
        validity=request.validity.value,
        quantity=request.quantity,
        price=request.price,
        trigger_price=request.trigger_price,
        status=OrderStatus.PENDING_RISK.value,
    )
    db.add(order)

    if not result.allowed:
        order.status = OrderStatus.REJECTED_RISK.value
        order.status_message = "; ".join(result.reasons)
        await db.commit()
        return order

    if environment == Environment.PAPER.value:
        adapter = get_adapter(account)
        ack = await adapter.place_order(request, client_order_id)
        order.broker_order_id = ack.broker_order_id
        order.status = OrderStatus.ACCEPTED.value
        await db.flush()
        # Immediate fill attempt so market orders feel live; rest on worker tick.
        await paper_engine.try_fill_order(db, redis, order)
        await db.commit()
        return order

    # ── live dispatch ────────────────────────────────────────────────
    refusal = _live_gate(account)
    if refusal:
        order.status = OrderStatus.FAILED.value
        order.status_message = f"Live trading blocked: {refusal}"
        await db.commit()
        return order

    adapter = get_adapter(account)
    try:
        ack = await adapter.place_order(request, client_order_id)
        order.broker_order_id = ack.broker_order_id
        order.status = ack.status.value
        order.status_message = ack.status_message
        await audit.emit(
            db,
            AuditEventType.BROKER_RESPONSE,
            user_id=user_id,
            entity_type="order",
            entity_id=order.id,
            correlation_id=client_order_id,
            payload={"response": ack.raw},
        )
    except (BrokerError, FeatureNotSupportedError) as e:
        order.status = OrderStatus.FAILED.value
        order.status_message = str(e)
        await audit.emit(
            db,
            AuditEventType.BROKER_RESPONSE,
            user_id=user_id,
            entity_type="order",
            entity_id=order.id,
            correlation_id=client_order_id,
            payload={"error": str(e)},
        )
    await db.commit()
    return order


async def cancel_order(
    db: AsyncSession, redis: aioredis.Redis, *, user_id: uuid.UUID, order: Order
) -> Order:
    account = await db.get(BrokerAccount, order.broker_account_id)
    if account is None:
        raise OrderServiceError("Broker account missing")

    if order.environment == Environment.PAPER.value:
        try:
            await paper_engine.cancel_order(db, order)
        except ValueError as e:
            raise OrderServiceError(str(e)) from e
        await db.commit()
        return order

    adapter = get_adapter(account)
    try:
        ack = await adapter.cancel_order(order.broker_order_id or "")
        old = order.status
        order.status = ack.status.value
        await audit.emit(
            db,
            AuditEventType.ORDER_STATE_CHANGED,
            user_id=user_id,
            entity_type="order",
            entity_id=order.id,
            correlation_id=order.client_order_id,
            payload={"from": old, "to": order.status, "via": "cancel"},
        )
    except (BrokerError, FeatureNotSupportedError) as e:
        raise OrderServiceError(str(e)) from e
    await db.commit()
    return order


async def modify_order(
    db: AsyncSession,
    redis: aioredis.Redis,
    *,
    user_id: uuid.UUID,
    order: Order,
    price: Decimal | None,
    quantity: int | None,
) -> Order:
    if not OrderStatus(order.status).is_working and order.status != OrderStatus.ACCEPTED.value:
        raise OrderServiceError(f"Cannot modify order in status {order.status}")
    if order.environment != Environment.PAPER.value:
        # TODO(live-modify): route through adapter.modify_order once any live
        # adapter is verified.
        raise OrderServiceError("Live order modification not supported yet")

    old = {"price": str(order.price), "quantity": order.quantity}
    if price is not None:
        order.price = price
    if quantity is not None:
        if quantity < (order.filled_quantity or 0):
            raise OrderServiceError("Quantity below already-filled amount")
        order.quantity = quantity
    await audit.emit(
        db,
        AuditEventType.ORDER_STATE_CHANGED,
        user_id=user_id,
        entity_type="order",
        entity_id=order.id,
        correlation_id=order.client_order_id,
        payload={"via": "modify", "from": old, "to": {"price": str(order.price), "quantity": order.quantity}},
    )
    await db.commit()
    return order
