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
from app.adapters.registry import get_trading_adapter
from app.core.config import get_settings
from app.db.models import BrokerAccount, IdempotencyKey, Order
from app.domain.capabilities import get_capabilities
from app.domain.enums import (
    AdapterStatus,
    AuditEventType,
    Broker,
    Environment,
    Exchange,
    OptionRight,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    Validity,
)
from app.domain.models import OrderRequest
from app.engines.paper import engine as paper_engine
from app.engines.risk.engine import RiskEngine
from app.services import audit, quotes


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
    # Step 1 of the pipeline, enforced here rather than trusted from callers:
    # the account must belong to the user the order is being placed for.
    # Callers verify ownership when they resolve the account; this is the
    # backstop that makes a missed check at any call site non-exploitable.
    if account.user_id != user_id:
        raise OrderServiceError("Broker account does not belong to this user — order refused")

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

    # Paper prices off the simulator; live prices off a real tick, or not at
    # all. market_sim.get_price invents a seed price for an unknown symbol,
    # which is right for a simulation and wrong for a live order — a risk
    # check that passes against a fabricated number is not a risk check.
    try:
        last_price = await quotes.reference_price(
            db,
            redis,
            account=account,
            symbol=request.symbol,
            exchange=request.exchange.value,
        )
    except quotes.NoQuoteAvailable as exc:
        await audit.emit(
            db,
            AuditEventType.RISK_CHECK,
            user_id=user_id,
            entity_type="order_intent",
            entity_id=client_order_id,
            correlation_id=client_order_id,
            payload={"decision": "HALT", "reason": str(exc)},
        )
        raise OrderServiceError(str(exc)) from exc

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
        # Remembered so modify and reconciliation know which contract this is.
        expiry=request.expiry,
        strike=request.strike,
        option_right=request.right.value if request.right else None,
        status=OrderStatus.PENDING_RISK.value,
    )
    db.add(order)

    if not result.allowed:
        order.status = OrderStatus.REJECTED_RISK.value
        order.status_message = "; ".join(result.reasons)
        await db.commit()
        return order

    if environment == Environment.PAPER.value:
        # get_trading_adapter, not get_adapter: this branch has already decided
        # the order is simulated, and get_adapter would hand back the real
        # broker's adapter regardless -- sending a live order that the paper
        # engine then books a fabricated fill for, two lines below.
        adapter = get_trading_adapter(account)
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

    adapter = get_trading_adapter(account)
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
    if order.user_id != user_id:
        raise OrderServiceError("Order does not belong to this user — cancel refused")
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

    adapter = get_trading_adapter(account)
    try:
        # The order's own exchange, not a default: Breeze needs exchange_code
        # on its cancel endpoint, and an NFO order cancelled as NSE is not
        # found. Parsed defensively -- a cancel must not fail because a stored
        # string is unexpected.
        try:
            order_exchange = Exchange(order.exchange)
        except ValueError:
            order_exchange = None
        ack = await adapter.cancel_order(order.broker_order_id or "", order_exchange)
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
    if order.user_id != user_id:
        raise OrderServiceError("Order does not belong to this user — modify refused")
    if order.environment == Environment.PAPER.value:
        await paper_engine.ledger.lock_account(db, order.broker_account_id)
        await db.refresh(order)
    if not OrderStatus(order.status).is_working and order.status != OrderStatus.ACCEPTED.value:
        raise OrderServiceError(f"Cannot modify order in status {order.status}")
    old = {"price": str(order.price), "quantity": order.quantity}

    # Validate before sending anything: a quantity below what is already
    # filled is incoherent at any broker, and finding that out from a rejected
    # modify would leave the local row and the broker disagreeing.
    if quantity is not None and quantity < (order.filled_quantity or 0):
        raise OrderServiceError("Quantity below already-filled amount")

    new_price = price if price is not None else order.price
    new_quantity = quantity if quantity is not None else order.quantity

    if order.environment != Environment.PAPER.value:
        # Live modify used to raise "not supported yet" outright, which made
        # adapter.modify_order unreachable. Amending a resting order is how a
        # limit that has stopped being marketable gets repriced; without it
        # the only options are to leave it or cancel and replace, and a
        # cancel-replace loses queue priority and can cross in between.
        #
        # Mirrors cancel_order: the broker is the source of truth, so it is
        # told first and the local row is updated only on acknowledgement. The
        # reverse order would leave our record claiming a price the broker
        # never accepted.
        account = await db.get(BrokerAccount, order.broker_account_id)
        if account is None:
            raise OrderServiceError("Broker account missing")
        refusal = _live_gate(account)
        if refusal:
            raise OrderServiceError(refusal)

        request = OrderRequest(
            symbol=order.symbol,
            exchange=Exchange(order.exchange),
            side=OrderSide(order.side),
            order_type=OrderType(order.order_type),
            product=ProductType(order.product),
            quantity=new_quantity,
            price=new_price,
            trigger_price=order.trigger_price,
            validity=Validity(order.validity),
            # Carried from the stored row: Breeze marks these mandatory on
            # PUT /order too, so an amend that dropped them would be rejected
            # for an F&O order.
            expiry=order.expiry,
            strike=order.strike,
            right=OptionRight(order.option_right) if order.option_right else None,
        )
        adapter = get_trading_adapter(account)
        try:
            await adapter.modify_order(order.broker_order_id or "", request)
        except (BrokerError, FeatureNotSupportedError) as e:
            raise OrderServiceError(str(e)) from e

    order.price = new_price
    order.quantity = new_quantity
    await audit.emit(
        db,
        AuditEventType.ORDER_STATE_CHANGED,
        user_id=user_id,
        entity_type="order",
        entity_id=order.id,
        correlation_id=order.client_order_id,
        payload={
            "via": "modify",
            "from": old,
            "to": {"price": str(order.price), "quantity": order.quantity},
        },
    )
    await db.commit()
    return order
