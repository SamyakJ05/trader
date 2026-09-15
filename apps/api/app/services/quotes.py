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

    price = await live_price(db, redis, symbol=symbol, exchange=exchange)
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
) -> Decimal | None:
    """The most recent real tick for a symbol, or None if there isn't a fresh
    one. Never invents a price."""
    cached = await _cached_tick(redis, symbol=symbol, exchange=exchange)
    if cached is not None:
        return cached
    # Fall back to the last completed candle's close. Older than a tick, but
    # real, and the staleness check still applies.
    return await candle_store.last_close(
        db, symbol=symbol, exchange=exchange, max_age_seconds=MAX_QUOTE_AGE_SECONDS
    )


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
