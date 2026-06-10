"""Simulated price feed for paper trading.

Deterministic seed per symbol -> believable base price, then a small
random walk per tick. Prices live in Redis so API and worker share state.
Replace with real broker market data (Kite WebSocket etc.) once a live
adapter is verified — the interface is just get_price / tick_all.
"""

import hashlib
import random
from decimal import ROUND_HALF_UP, Decimal

import redis.asyncio as aioredis

PRICE_KEY = "sim:px:{symbol}"
SYMBOLS_KEY = "sim:symbols"
HISTORY_KEY = "sim:hist:{symbol}"
HISTORY_LEN = 500

_TICK = Decimal("0.05")


def _seed_price(symbol: str) -> Decimal:
    digest = int(hashlib.sha256(symbol.encode()).hexdigest(), 16)
    base = Decimal(100 + digest % 2400)  # 100..2499
    return base.quantize(Decimal("0.05"))


def _round_tick(price: Decimal) -> Decimal:
    steps = (price / _TICK).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (steps * _TICK).quantize(Decimal("0.01"))


async def get_price(redis: aioredis.Redis, symbol: str) -> Decimal:
    key = PRICE_KEY.format(symbol=symbol)
    raw = await redis.get(key)
    if raw is None:
        price = _seed_price(symbol)
        await redis.set(key, str(price))
        await redis.sadd(SYMBOLS_KEY, symbol)
        return price
    return Decimal(raw)


async def tick_all(redis: aioredis.Redis) -> dict[str, Decimal]:
    """Advance every tracked symbol one step. Returns new prices."""
    symbols = await redis.smembers(SYMBOLS_KEY)
    out: dict[str, Decimal] = {}
    for symbol in symbols:
        current = await get_price(redis, symbol)
        drift = Decimal(str(random.gauss(0, 0.0012)))
        new_price = _round_tick(max(current * (1 + drift), _TICK))
        await redis.set(PRICE_KEY.format(symbol=symbol), str(new_price))
        hist_key = HISTORY_KEY.format(symbol=symbol)
        await redis.rpush(hist_key, str(new_price))
        await redis.ltrim(hist_key, -HISTORY_LEN, -1)
        out[symbol] = new_price
    return out


async def get_history(redis: aioredis.Redis, symbol: str, n: int) -> list[Decimal]:
    raw = await redis.lrange(HISTORY_KEY.format(symbol=symbol), -n, -1)
    return [Decimal(x) for x in raw]
