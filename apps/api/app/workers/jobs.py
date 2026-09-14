"""Background jobs. Tick cadence drives the paper world:
price step -> fill open paper orders -> mark positions -> run strategies."""

from datetime import datetime, timezone

from app.domain.models import Tick
from app.domain.enums import Exchange
from app.engines.market.candles import record_tick
from app.engines.paper.settlement import settle_due
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.db.session import async_session_factory
from app.engines.paper import engine as paper_engine
from app.engines.paper import market_sim
from app.engines.strategy import runner

logger = get_logger(__name__)


async def paper_tick(ctx: dict) -> None:
    redis = get_redis()
    prices = await market_sim.tick_all(redis)
    async with async_session_factory() as db:
        await settle_due(db)
        await db.commit()
        now = datetime.now(timezone.utc)
        for symbol, price in prices.items():
            await record_tick(
                db, Tick(symbol=symbol, exchange=Exchange.NSE, last_price=price, ts=now)
            )
        filled = await paper_engine.process_open_orders(db, redis)
        await paper_engine.mark_positions(db, redis)
        await db.commit()
    if filled:
        logger.info("paper_tick", symbols=len(prices), orders_filled=filled)


async def strategy_tick(ctx: dict) -> None:
    redis = get_redis()
    async with async_session_factory() as db:
        emitted = await runner.run_once(db, redis)
    if emitted:
        logger.info("strategy_tick", signals=emitted)
