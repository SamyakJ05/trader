"""Reference prices for risk checks.

Every order is valued before it is allowed: notional limits, position sizing
and daily-loss checks all need a price. Where that price comes from differs
by environment, and conflating them is dangerous in one direction.

The paper simulator invents a seed price for any symbol it has not seen, which
is right for a simulation and wrong for a live order: a risk check that passes
against a fabricated number is not a risk check. Live accounts therefore get a
real quote or no order at all.
"""

from decimal import Decimal

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import BrokerAccount
from app.domain.enums import Environment
from app.engines.market import candles as candle_store
from app.engines.paper import market_sim

logger = get_logger(__name__)

# How stale a stored tick may be and still price a live order. A quote from
# before lunch says nothing about the market now, and valuing an order against
# it could pass a limit the real price would fail.
MAX_QUOTE_AGE_SECONDS = 120


class NoQuoteAvailable(Exception):
    """Raised when a live order cannot be priced.

    Refusing the order is the correct outcome: the alternative is a risk check
    against a number nobody stands behind.
    """


async def reference_price(
    db: AsyncSession,
    redis: aioredis.Redis,
    *,
    account: BrokerAccount,
    symbol: str,
    exchange: str = "NSE",
) -> Decimal:
    """The price to value an order at, for this account's environment.

    Paper uses the simulator, which is the whole point of paper. Live uses the
    most recent real tick, and refuses rather than falling back — a live order
    priced off the simulator would be valued against a number with no
    relationship to the market.
    """
    if account.environment != Environment.LIVE.value:
        return await market_sim.get_price(redis, symbol)

    price = await live_price(
        db, redis, symbol=symbol, exchange=exchange, account=account
    )
    if price is None:
        raise NoQuoteAvailable(
            f"No recent market price for {symbol} on {exchange}. "
            "A live order cannot be risk-checked without one."
        )
    return price


async def live_price(
    db: AsyncSession,
    redis: aioredis.Redis,
    *,
    symbol: str,
    exchange: str = "NSE",
    account: BrokerAccount | None = None,
) -> Decimal | None:
    """The most recent real price for a symbol, or None if there isn't one.

    Three sources, in descending freshness, every one of them the broker's
    own number -- this never invents a price:

      1. A cached websocket tick, held with a TTL so an expired one cannot be
         read as current.
      2. The last completed candle's close, subject to the same staleness
         limit.
      3. A REST quote from the broker, when an account is given.

    The third exists because the tick stream only subscribes to symbols a
    strategy names. A live order for anything else -- the usual case when
    placing one by hand -- would otherwise be refused for want of a price
    even with the market open and the session healthy. It costs one API call
    against the broker's rate budget and is tried last for that reason.
    """
    cached = await _cached_tick(redis, symbol=symbol, exchange=exchange)
    if cached is not None:
        return cached
    # Fall back to the last completed candle's close. Older than a tick, but
    # real, and the staleness check still applies.
    close = await candle_store.last_close(
        db, symbol=symbol, exchange=exchange, max_age_seconds=MAX_QUOTE_AGE_SECONDS
    )
    if close is not None:
        return close

    if account is None:
        return None
    quote = await _broker_quote(account, symbol=symbol, exchange=exchange)
    if quote is not None:
        # Cached like a tick, so a burst of orders in the same symbol does not
        # spend one API call each against a tight per-minute budget.
        await record_live_tick(redis, symbol=symbol, exchange=exchange, price=quote)
    return quote


async def _broker_quote(
    account: BrokerAccount, *, symbol: str, exchange: str
) -> Decimal | None:
    """A REST quote from the account's broker, or None if it cannot supply one.

    Adapters are not required to implement get_quote; one that does not simply
    has no third source. Failures are swallowed deliberately -- the caller
    refuses the order for want of a price either way, and a broker being slow
    should not surface as something other than "no quote".
    """
    from app.adapters.registry import get_adapter

    try:
        adapter = get_adapter(account)
    except Exception:
        logger.warning("quote_adapter_unavailable", symbol=symbol, exc_info=True)
        return None
    getter = getattr(adapter, "get_quote", None)
    if getter is None:
        return None
    try:
        return await getter(symbol, exchange)
    except Exception:
        logger.warning("quote_lookup_failed", symbol=symbol, exc_info=True)
        return None


def _live_key(symbol: str, exchange: str) -> str:
    return f"quote:live:{exchange}:{symbol}"


async def record_live_tick(
    redis: aioredis.Redis, *, symbol: str, exchange: str, price: Decimal
) -> None:
    """Cache a real tick for risk checks.

    Written with a TTL rather than a timestamp: an expired key is
    indistinguishable from an absent one, so a stale quote cannot be read by a
    caller that forgot to check its age.
    """
    await redis.setex(_live_key(symbol, exchange), MAX_QUOTE_AGE_SECONDS, str(price))


async def _cached_tick(
    redis: aioredis.Redis, *, symbol: str, exchange: str
) -> Decimal | None:
    raw = await redis.get(_live_key(symbol, exchange))
    if raw is None:
        return None
    try:
        price = Decimal(raw)
    except ArithmeticError:
        return None
    return price if price.is_finite() and price > 0 else None
