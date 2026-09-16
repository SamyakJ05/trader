"""Live tick ingestion.

The Kite socket is long-lived, so this is a supervised service rather than a
cron job: it connects once, stays connected, and reconnects when the session or
the socket drops. Everything a tick feeds — the quote cache that risk checks
read, and the candles strategies and backtests read — is written here.

One stream per connected live account. Accounts share a process, so a failure
on one must not take down the others, on the same reasoning as the paper tick.
"""

import asyncio
import time

from sqlalchemy import select

from app.adapters.base import SessionExpiredError
from app.adapters.registry import get_adapter
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.db.models import BrokerAccount, Strategy
from app.db.session import async_session_factory
from app.domain.enums import Broker, BrokerAccountStatus, Environment
from app.engines.market.candles import record_tick
from app.services import instruments as instrument_service
from app.services import quotes

logger = get_logger(__name__)

# How long to wait before retrying a stream that dropped. Long enough not to
# hammer the broker through an outage, short enough that a transient blip
# does not cost a session of prices.
RECONNECT_DELAY_SECONDS = 15

# How long to wait after a session expiry before looking again. Longer than a
# reconnect: nothing can succeed until a human logs in through the broker, so
# polling hard would only burn database reads for hours.
SESSION_RETRY_DELAY_SECONDS = 300

# How often an open feed rechecks which instruments its account's strategies
# need. A feed subscribes once at open, so without this a strategy added later
# never receives a price.
SYMBOL_RECHECK_SECONDS = 60

# Ticks are recorded as candles under this source, which keeps live data and
# imported history distinguishable — a backtest must be able to say which it
# ran on.
# Candles from a live feed are tagged by broker, so a backtest can say which
# feed it ran on and two brokers' data for the same instrument never merge —
# they do not even use the same codes.
LIVE_SOURCES = {
    Broker.ZERODHA.value: "kite",
    Broker.ICICI_BREEZE.value: "breeze",
}

# Retained for callers that predate per-broker sources.
LIVE_SOURCE = LIVE_SOURCES[Broker.ZERODHA.value]

_STREAMING_BROKERS = set(LIVE_SOURCES)


async def _symbols_for(db, account: BrokerAccount) -> list[str]:
    """Which instruments this account needs prices for.

    Only what its strategies actually trade: subscribing to everything would
    burn the broker's instrument quota and fill the database with candles
    nobody reads.
    """
    result = await db.execute(
        select(Strategy.symbols).where(
            Strategy.broker_account_id == account.id,
            Strategy.status.in_(["RUNNING", "DRAFT"]),
        )
    )
    symbols: set[str] = set()
    for row in result.scalars():
        symbols.update(row or [])
    return sorted(symbols)


async def stream_account(account_id) -> None:
    """Keep one account's tick stream running until cancelled."""
    while True:
        try:
            await _run_once(account_id)
        except asyncio.CancelledError:
            raise
        except SessionExpiredError:
            # Nothing to retry until the user logs in again, and the session
            # job will already have marked the account -- but this used to
            # `return`, ending the task for good. The supervisor only starts a
            # task when there isn't one, so after the user re-authenticated
            # the next morning the stream stayed dead until the whole worker
            # was restarted: a silent, daily loss of the live feed.
            #
            # Waiting and looping instead costs one no-op database read per
            # interval. _run_once returns immediately while the account is not
            # CONNECTED, so this idles quietly until the session comes back
            # and then reconnects on its own.
            logger.info("tick_stream_session_expired", broker_account_id=str(account_id))
            await asyncio.sleep(SESSION_RETRY_DELAY_SECONDS)
            continue
        except Exception:
            logger.exception("tick_stream_failed", broker_account_id=str(account_id))
        await asyncio.sleep(RECONNECT_DELAY_SECONDS)


async def _run_once(account_id) -> None:
    async with async_session_factory() as db:
        account = await db.get(BrokerAccount, account_id)
        if account is None or account.status != BrokerAccountStatus.CONNECTED.value:
            return
        symbols = await _symbols_for(db, account)
        if not symbols:
            logger.info("tick_stream_idle", broker_account_id=str(account_id))
            return
        token_map = await instrument_service.token_map(
            db, broker=account.broker, symbols=symbols
        )
        if not token_map:
            # Without the instrument master there is nothing to subscribe to.
            # Say so rather than opening a socket that receives nothing.
            logger.warning(
                "tick_stream_no_tokens",
                broker_account_id=str(account_id),
                symbols=symbols,
            )
            return

        adapter = get_adapter(account)
        source = LIVE_SOURCES.get(account.broker, LIVE_SOURCE)
        feed = await adapter.tick_feed(token_map)

    redis = get_redis()
    subscribed = set(symbols)
    next_symbol_check = time.monotonic() + SYMBOL_RECHECK_SECONDS
    try:
        async for tick in feed.ticks():
            # A feed subscribes once, to the symbols that existed when it
            # opened. A strategy added afterwards would get no prices until
            # the worker was restarted -- and the runner refuses to trade
            # live without a fresh quote, so the new strategy would sit
            # silently dead. Returning drops back into stream_account's loop,
            # which reopens the feed with the current set.
            #
            # Checked on a timer rather than per tick: this runs on every
            # tick of every instrument, and a database round trip there would
            # be its own outage.
            now = time.monotonic()
            if now >= next_symbol_check:
                next_symbol_check = now + SYMBOL_RECHECK_SECONDS
                async with async_session_factory() as db:
                    account = await db.get(BrokerAccount, account_id)
                    current = set(await _symbols_for(db, account)) if account else set()
                if current != subscribed:
                    logger.info(
                        "tick_stream_resubscribing",
                        broker_account_id=str(account_id),
                        added=sorted(current - subscribed),
                        removed=sorted(subscribed - current),
                    )
                    return
            # The quote cache first: it is what stops a live order being
            # risk-checked against a price nobody stands behind, and it must
            # not wait on a database write.
            await quotes.record_live_tick(
                redis,
                symbol=tick.symbol,
                exchange=tick.exchange.value,
                price=tick.last_price,
            )
            async with async_session_factory() as db:
                try:
                    await record_tick(db, tick, source=source)
                    await db.commit()
                except Exception:
                    await db.rollback()
                    logger.exception("tick_candle_failed", symbol=tick.symbol)
    finally:
        await feed.stop()


async def live_accounts(db) -> list[BrokerAccount]:
    """Connected live accounts on brokers that can stream."""
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.broker.in_(_STREAMING_BROKERS),
            BrokerAccount.environment == Environment.LIVE.value,
            BrokerAccount.status == BrokerAccountStatus.CONNECTED.value,
        )
    )
    return list(result.scalars())


async def run_forever() -> None:
    """Supervise a stream per live account.

    Rechecks periodically so an account connected after startup begins
    streaming without a restart, and one that disconnects stops.
    """
    tasks: dict[str, asyncio.Task] = {}
    try:
        while True:
            async with async_session_factory() as db:
                accounts = await live_accounts(db)
            wanted = {str(a.id) for a in accounts}

            for account in accounts:
                key = str(account.id)
                task = tasks.get(key)
                if task is None or task.done():
                    tasks[key] = asyncio.create_task(stream_account(account.id))
                    logger.info("tick_stream_started", broker_account_id=key)

            for key, task in list(tasks.items()):
                if key not in wanted:
                    task.cancel()
                    tasks.pop(key, None)
                    logger.info("tick_stream_stopped", broker_account_id=key)

            await asyncio.sleep(30)
    finally:
        for task in tasks.values():
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)
