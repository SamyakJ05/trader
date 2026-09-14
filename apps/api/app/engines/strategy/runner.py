"""Strategy runner — orchestrates RUNNING strategies on each worker tick.

Per strategy: pull price history -> evaluate -> persist signals -> route
each signal through the standard order pipeline (risk checks included).
Kill switches short-circuit before evaluation."""

import uuid

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import BrokerAccount, Position, Strategy, TradingSignal, PaperHolding
from app.domain.enums import (
    AuditEventType,
    Exchange,
    OrderSide,
    OrderType,
    ProductType,
    SignalType,
    StrategyStatus,
)
from app.domain.models import OrderRequest
from app.engines.paper import market_sim
from app.engines.market.candles import history as candle_history
from app.engines.strategy.ai_agent import AiAgentStrategy
from app.engines.strategy.base import AsyncStrategyBase, Signal, StrategyBase, StrategyContext
from app.engines.strategy.sma_crossover import SmaCrossover
from app.services import audit, killswitch
from app.services import orders as order_service

logger = get_logger(__name__)

STRATEGY_REGISTRY: dict[str, StrategyBase] = {
    SmaCrossover.kind: SmaCrossover(),
    AiAgentStrategy.kind: AiAgentStrategy(),
}

_SIGNAL_SIDE = {
    SignalType.ENTRY_LONG: OrderSide.BUY,
    SignalType.EXIT_LONG: OrderSide.SELL,
    SignalType.ENTRY_SHORT: OrderSide.SELL,
    SignalType.EXIT_SHORT: OrderSide.BUY,
}


async def _position_quantity(
    db: AsyncSession, account_id: uuid.UUID, symbol: str, exchange="NSE", product="MIS"
) -> int:
    result = await db.execute(
        select(Position).where(
            Position.broker_account_id == account_id,
            Position.symbol == symbol,
            Position.exchange == exchange,
            Position.product == product,
        )
    )
    position = result.scalar_one_or_none()
    quantity = position.quantity if position else 0
    if product == "CNC":
        holding = (
            await db.execute(
                select(PaperHolding).where(
                    PaperHolding.broker_account_id == account_id,
                    PaperHolding.symbol == symbol,
                    PaperHolding.exchange == exchange,
                )
            )
        ).scalar_one_or_none()
        quantity += holding.quantity if holding else 0
    return quantity


async def _act_on_signal(
    db: AsyncSession,
    redis: aioredis.Redis,
    strategy: Strategy,
    account: BrokerAccount,
    signal: Signal,
) -> None:
    record = TradingSignal(
        strategy_id=strategy.id,
        symbol=signal.symbol,
        exchange=strategy.params.get("exchange", "NSE"),
        signal_type=signal.signal_type.value,
        payload={"note": signal.note, "quantity": signal.quantity},
    )
    db.add(record)
    await audit.emit(
        db,
        AuditEventType.SIGNAL_GENERATED,
        user_id=strategy.user_id,
        entity_type="strategy",
        entity_id=strategy.id,
        payload={"signal": signal.model_dump()},
    )
    await db.flush()

    request = OrderRequest(
        symbol=signal.symbol,
        exchange=Exchange(strategy.params.get("exchange", "NSE")),
        side=_SIGNAL_SIDE[signal.signal_type],
        order_type=OrderType.MARKET,
        product=ProductType(strategy.params.get("product", "MIS")),
        quantity=signal.quantity,
    )
    client_order_id = f"st-{strategy.id.hex[:8]}-{uuid.uuid4().hex[:10]}"
    order = await order_service.place_order(
        db,
        redis,
        user_id=strategy.user_id,
        account=account,
        request=request,
        client_order_id=client_order_id,
        strategy_id=strategy.id,
    )
    record.acted = True
    record.order_id = order.id
    await db.commit()


async def run_once(db: AsyncSession, redis: aioredis.Redis) -> int:
    """One orchestration pass over all RUNNING strategies. Returns signal count."""
    if await killswitch.is_global_engaged(redis):
        return 0

    result = await db.execute(
        select(Strategy).where(Strategy.status == StrategyStatus.RUNNING.value)
    )
    strategies = result.scalars().all()
    emitted = 0

    for strategy in strategies:
        strategy = (
            await db.execute(
                select(Strategy)
                .where(
                    Strategy.id == strategy.id,
                    Strategy.status == StrategyStatus.RUNNING.value,
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if strategy is None:
            continue
        if await killswitch.is_strategy_engaged(redis, strategy.id):
            continue
        impl = STRATEGY_REGISTRY.get(strategy.kind)
        if impl is None:
            logger.warning("unknown_strategy_kind", kind=strategy.kind, id=str(strategy.id))
            continue
        if strategy.broker_account_id is None:
            continue
        account = await db.get(BrokerAccount, strategy.broker_account_id)
        if account is None:
            continue

        for symbol in strategy.symbols:
            await market_sim.get_price(redis, symbol)  # ensure tracked by the feed
            candles = await candle_history(
                db,
                symbol,
                strategy.params.get("exchange", "NSE"),
                strategy.params.get("interval", "1m"),
                strategy.params.get("source", "simulator"),
                impl.min_history(strategy.params),
            )
            history = [candle.close for candle in candles]
            if len(history) < impl.min_history(strategy.params):
                continue
            # Persist a per-symbol cursor in the strategy transaction. Restarting
            # a worker must not repeatedly trade the same completed candle.
            cursor = dict(strategy.params.get("_candle_cursors", {}))
            stamp = candles[-1].ts.isoformat()
            if cursor.get(symbol) == stamp:
                continue
            cursor[symbol] = stamp
            strategy.params = {**strategy.params, "_candle_cursors": cursor}
            ctx = StrategyContext(
                symbol=symbol,
                prices=history,
                position_quantity=await _position_quantity(
                    db,
                    account.id,
                    symbol,
                    strategy.params.get("exchange", "NSE"),
                    strategy.params.get("product", "MIS"),
                ),
                # _strategy_id/_user_id let async strategies (ai_agent) resolve
                # their LLM config and rate-limit keys without schema changes.
                params={
                    **strategy.params,
                    "_strategy_id": str(strategy.id),
                    "_user_id": str(strategy.user_id),
                },
            )
            try:
                if isinstance(impl, AsyncStrategyBase):
                    signals = await impl.evaluate_async(ctx, db=db, redis=redis)
                else:
                    signals = impl.evaluate(ctx)
            except Exception:
                logger.exception("strategy_evaluate_failed", strategy=str(strategy.id))
                strategy.status = StrategyStatus.ERROR.value
                await db.commit()
                break
            for signal in signals:
                # One strategy's failure must not abort the tick for every
                # other user: the runner loop is shared across all tenants.
                try:
                    await _act_on_signal(db, redis, strategy, account, signal)
                    emitted += 1
                except Exception:
                    logger.exception(
                        "strategy_signal_failed",
                        strategy=str(strategy.id),
                        symbol=signal.symbol,
                    )
                    await db.rollback()
                    strategy.status = StrategyStatus.ERROR.value
                    await db.commit()
                    break
    await db.commit()
    return emitted
