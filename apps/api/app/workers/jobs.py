"""Background jobs. Tick cadence drives the paper world:
price step -> fill open paper orders -> mark positions -> run strategies."""

from datetime import datetime, timezone

from app.core.config import get_settings
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
from app.services import bhavcopy, heartbeat, news, order_reconcile
from app.services.ai import research
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

    # The heartbeat above is stamped FIRST and unconditionally: /readyz reads
    # it to decide whether the worker is alive, and a live-only instance whose
    # worker looked dead would be a false alarm about the process that runs
    # the tick stream and the fill reconciler.
    #
    # Everything below this line is the paper simulation -- price steps,
    # settlement, simulated fills, position marks. None of it should run when
    # the instance is live-only.
    if not get_settings().enable_paper_trading:
        return

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


async def instrument_sync_tick(ctx: dict) -> None:
    """Refresh each connected broker's instrument master.

    Nothing in the live path works without this. The master is the only
    mapping from a trading symbol to the broker's own token: the tick stream
    subscribes by token, so an empty master means a socket that receives
    nothing, and strategy creation rejects every symbol as unknown. The
    service existed and was fully written; it simply had no caller, so on a
    fresh deployment the table stayed empty forever.

    Breeze regenerates its security master around 08:00 IST and Kite's is
    refreshed daily; derivative tokens are not stable across days. Running at
    08:30 IST picks up both after they are published and before the market
    opens.

    One account per broker is enough -- the master is the broker's, not the
    account's -- so this syncs the first connected account of each broker
    rather than repeating a multi-megabyte download per user.
    """
    from sqlalchemy import select

    from app.db.models import BrokerAccount
    from app.domain.enums import BrokerAccountStatus
    from app.services.instruments import sync_instruments

    async with async_session_factory() as db:
        result = await db.execute(
            select(BrokerAccount)
            .where(BrokerAccount.status == BrokerAccountStatus.CONNECTED.value)
            .order_by(BrokerAccount.created_at)
        )
        accounts = list(result.scalars())

    done: set[str] = set()
    for account in accounts:
        if account.broker in done:
            continue
        done.add(account.broker)
        # NFO carries ~80k contracts and is what makes F&O tradable at all.
        # BSE is deliberately absent for Breeze: ICICI's own documentation
        # says "securities listed on BSE and MCX are not available on Breeze
        # API", so syncing it downloads rows no Breeze order could ever use.
        exchanges = ("NSE", "NFO") if account.broker == "icici_breeze" else ("NSE", "BSE")
        for exchange in exchanges:
            # Per exchange and per account: one broker's download failing --
            # or one exchange being unavailable -- must not cost the others
            # their sync. A stale master is bad; no master at all is worse.
            async with async_session_factory() as db:
                try:
                    written = await sync_instruments(db, account, exchange=exchange)
                    await db.commit()
                    logger.info(
                        "instrument_sync",
                        broker=account.broker,
                        exchange=exchange,
                        rows=written,
                    )
                except Exception:
                    await db.rollback()
                    logger.exception(
                        "instrument_sync_failed",
                        broker=account.broker,
                        exchange=exchange,
                    )


async def order_reconcile_tick(ctx: dict) -> None:
    """Ask polled brokers what happened to our open live orders.

    Breeze does not push order state -- its capability matrix says so and it
    is accurate -- so without this a live order that filled updated nothing:
    no Fill row, no position, no cash movement, and no realized-P&L, which is
    what MAX_DAILY_LOSS reads. An account could lose any amount and the limit
    would never fire.

    Every 30s during the trading day. Frequent enough that a strategy acting
    on its own position size is not working from hours-old information, and
    cheap enough against Breeze's 100/min budget: one call per connected live
    account per cycle, only when that account has open orders.
    """
    redis = get_redis()
    try:
        changed = await order_reconcile.reconcile_all(async_session_factory, redis)
    except Exception:
        logger.exception("order_reconcile_tick_failed")
        return
    if changed:
        logger.info("order_reconcile_tick", orders=changed)


async def import_history_job(
    ctx: dict,
    *,
    user_id: str,
    symbols: list[str],
    interval: str,
    start: str,
    end: str,
) -> dict:
    """Import market history for one or more symbols.

    An ad-hoc job rather than a request handler: the download is a slow bulk
    fetch from Yahoo, and a year of daily bars across several symbols would
    hold an HTTP connection open long enough to time out behind the proxy.

    Each symbol is committed on its own. One bad ticker -- a delisting, a
    typo, a range Yahoo has no data for -- must not discard the symbols that
    imported cleanly before it, because the operator would have no way to
    tell which ones landed.
    """
    from datetime import date as _date

    from app.services.history import import_symbol

    imported, failed = [], []
    for symbol in symbols:
        async with async_session_factory() as db:
            try:
                report = await import_symbol(
                    db,
                    symbol=symbol,
                    interval=interval,
                    start=_date.fromisoformat(start),
                    end=_date.fromisoformat(end),
                )
                imported.append(report)
            except Exception as exc:
                await db.rollback()
                logger.warning(
                    "history_import_failed", symbol=symbol, error=str(exc)
                )
                # The message is the operator's only clue, and Yahoo's are
                # usually actionable ("no data found for this date range").
                failed.append({"symbol": symbol, "error": str(exc)[:300]})
    return {"imported": imported, "failed": failed}


async def bhavcopy_tick(ctx: dict) -> None:
    """Import the previous session's closes for every NSE equity.

    This is the screening universe. Without it, "which stocks can this
    account afford" can only be answered by pricing symbols one at a time
    through the broker, which spends quota and cannot rank what it finds.

    Never raises. It is not on the order path -- every order still prices off
    a live broker quote -- so a failed download must not mark the worker
    unhealthy or interrupt the ticks that move money. A missing file is
    normal: holidays have none, and the day's file is not published until
    after the close.
    """
    try:
        async with async_session_factory() as db:
            await bhavcopy.import_day(db)
    except bhavcopy.BhavcopyUnavailable as e:
        logger.info("bhavcopy_unavailable", detail=str(e))
    except Exception as e:
        logger.warning("bhavcopy_import_failed", error=str(e))


async def ai_research_tick(ctx: dict) -> None:
    """The daily AI research pass, before the open.

    Produces proposals a human approves -- or, on an account with
    auto_execute on, orders through the ordinary auto-execute path, with the
    same risk engine and daily cap as any other order.

    Never raises, for the same reason news_refresh_tick does not: this is
    advisory. A provider outage or a rate limit must not mark the worker
    unhealthy or interrupt the ticks that do move money.
    """
    try:
        async with async_session_factory() as db:
            await research.research_all_accounts(db, get_redis())
    except Exception as e:
        logger.warning("ai_research_tick_failed", error=str(e))


async def news_refresh_tick(ctx: dict) -> None:
    """Pull corporate announcements and financial headlines.

    Advisory only: the sole reader is the AI analyst's get_news tool, and the
    analyst's output is a proposal a human approves. Nothing here reaches a
    strategy or the order pipeline.

    Never raises. News is context, not a dependency -- a publisher being down
    must not mark the worker unhealthy or interrupt the ticks that do move
    money.
    """
    try:
        async with async_session_factory() as db:
            await news.refresh(db)
    except Exception as e:  # Advisory feed: logged, never escalated.
        logger.warning("news_refresh_failed", error=str(e))
