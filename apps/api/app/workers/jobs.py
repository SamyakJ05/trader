"""Background jobs. Tick cadence drives the paper world:
price step -> fill open paper orders -> mark positions -> run strategies."""

from datetime import datetime, timezone

from app.core.logging import get_logger
from app.core.redis import get_redis
from app.db.session import async_session_factory
from app.domain.enums import Exchange
from app.domain.models import Tick
from app.engines.market.candles import record_tick
from app.engines.paper import engine as paper_engine
from app.engines.paper import market_sim
from app.engines.paper.settlement import settle_due
from app.engines.strategy import runner
from app.services import heartbeat
from app.services.sessions_broker import expire_stale_sessions

logger = get_logger(__name__)


async def paper_tick(ctx: dict) -> None:
    """The platform's shared heartbeat: price step, settlement, fills, marks.

    Every stage is guarded independently. This one job drives paper trading for
    every user on the instance, so a failure in one stage -- or in one account
    inside a stage -- must not stop the others, and must not stop the next
    tick. Settlement and fills isolate per account and per order internally.
    """
    redis = get_redis()
    # Stamped before any stage that can throw. Liveness means "the worker is
    # running its loop", which is a different question from "every stage
    # succeeded" -- a persistent failure in one stage is a bug to fix, not a
    # reason for the process to be reported as dead.
    await heartbeat.beat(redis)
    # Guarded like every other stage. It runs first, so an unguarded failure
    # here would take settlement, fills and marks down with it -- and
    # settlement does not depend on prices at all: T+1 holdings becoming
    # available must not wait on a price feed.
    prices: dict = {}
    try:
        prices = await market_sim.tick_all(redis)
    except Exception:
        logger.exception("price_stage_failed")
    filled = 0
    async with async_session_factory() as db:
        try:
            await settle_due(db)
        except Exception:
            await db.rollback()
            logger.exception("settlement_stage_failed")

        now = datetime.now(timezone.utc)
        try:
            for symbol, price in prices.items():
                await record_tick(
                    db, Tick(symbol=symbol, exchange=Exchange.NSE, last_price=price, ts=now)
                )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("candle_stage_failed")

        try:
            filled = await paper_engine.process_open_orders(db, redis)
        except Exception:
            await db.rollback()
            logger.exception("fill_stage_failed")

        try:
            await paper_engine.mark_positions(db, redis)
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("mark_stage_failed")
    if filled:
        logger.info("paper_tick", symbols=len(prices), orders_filled=filled)


async def strategy_tick(ctx: dict) -> None:
    redis = get_redis()
    async with async_session_factory() as db:
        emitted = await runner.run_once(db, redis)
    if emitted:
        logger.info("strategy_tick", signals=emitted)


async def broker_session_tick(ctx: dict) -> None:
    """Mark broker sessions that the daily exchange flush has invalidated.

    Runs a few times an hour rather than once at the flush: a worker that was
    down at 6am would otherwise leave every account claiming to be connected
    until the next morning.
    """
    async with async_session_factory() as db:
        try:
            await expire_stale_sessions(db)
        except Exception:
            await db.rollback()
            logger.exception("broker_session_tick_failed")
