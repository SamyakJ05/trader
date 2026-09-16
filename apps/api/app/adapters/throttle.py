"""Client-side rate limiting for broker APIs.

Broker limits are per application, not per process: the API and every worker
share one budget, and exceeding it earns a block on the whole app rather than a
retry on one request. An in-process limiter would therefore be wrong by however
many processes are running.

This is a token bucket in Redis. Tokens refill continuously at the configured
rate, so a caller that has been idle may burst up to the bucket size and a busy
one settles to the steady rate — which is how the limits are actually enforced
at the other end.

The whole bucket update is one Lua script so it is atomic: read, refill and
take happen without another worker interleaving between them. Doing it in
round trips would let two workers both see one token left and both take it.
"""

import asyncio
import time

import redis.asyncio as aioredis

from app.core.logging import get_logger

logger = get_logger(__name__)

# Take a token if one is available; otherwise report how long until one is.
# KEYS[1] bucket, ARGV: rate per second, burst, cost.
#
# The clock is read with redis.call('TIME') inside the script, not passed in
# from Python. At a high rate (Kite's quote bucket is 1000/s in tests) even a
# few microseconds of asyncio dispatch jitter between concurrent callers is
# enough real time for a fractional token to refill -- so two callers with
# their own now values could each see room for one more than the burst
# actually allows, even though the read-refill-take itself is atomic. One
# shared clock read inside the same atomic script closes that gap.
_TAKE = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local time_parts = redis.call('TIME')
local now = tonumber(time_parts[1]) + tonumber(time_parts[2]) / 1000000

local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then
  tokens = burst
  ts = now
end

tokens = math.min(burst, tokens + (now - ts) * rate)
local wait = 0
if tokens >= cost then
  tokens = tokens - cost
else
  wait = (cost - tokens) / rate
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now)
-- Expire once a full bucket would have refilled; an idle key holds no useful
-- state and should not linger.
redis.call('EXPIRE', key, math.ceil(burst / rate) + 1)
return tostring(wait)
"""


class RateLimiter:
    """A named token bucket shared across processes."""

    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        name: str,
        rate_per_second: float,
        burst: int | None = None,
    ):
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self._redis = redis
        self._name = name
        self._rate = float(rate_per_second)
        # Default burst of one second's worth, minimum 1: enough to absorb
        # jitter without letting an idle caller dump a long backlog at once.
        self._burst = burst if burst is not None else max(1, int(rate_per_second))
        self._script = redis.register_script(_TAKE)

    @property
    def key(self) -> str:
        return f"throttle:{self._name}"

    async def _try_take(self, cost: int) -> float:
        """Seconds to wait before a token is available; 0 means taken."""
        raw = await self._script(
            keys=[self.key],
            args=[self._rate, self._burst, cost],
        )
        return float(raw)

    async def acquire(self, cost: int = 1, *, timeout: float = 30.0) -> None:
        """Wait until a token is available, then take it.

        Waits rather than failing: a broker call that is merely early should be
        delayed, not lost. `timeout` bounds that so a wedged bucket surfaces as
        an error instead of a request that hangs forever.
        """
        deadline = time.monotonic() + timeout
        waited = 0.0
        while True:
            wait = await self._try_take(cost)
            if wait <= 0:
                if waited > 0.5:
                    logger.info("throttle_delayed", bucket=self._name, seconds=round(waited, 2))
                return
            if time.monotonic() + wait > deadline:
                raise ThrottleTimeout(
                    f"Rate limit for {self._name} would need {wait:.1f}s, "
                    f"beyond the {timeout:.0f}s budget"
                )
            # Sleep a little past the computed wait: sleeping exactly to the
            # boundary tends to wake a hair early and spin.
            await asyncio.sleep(wait + 0.005)
            waited += wait


class ThrottleTimeout(Exception):
    """Raised when waiting for a token would exceed the caller's budget."""


# Kite's documented per-second limits, from their rate-limit table.
#
# The previous values had this backwards in both directions, on the stated
# belief that "quotes are allowed a higher rate than everything else". Quote
# is the MOST restricted endpoint Kite publishes, at 1/s -- it was set to 10,
# ten times over, which is the one that bites first because quote polling is
# the highest-frequency call a strategy makes. Meanwhile "default" was 3/s
# against a documented 10/s for all other endpoints, needlessly throttling
# /user and /portfolio reads.
#
# Exceeding these earns an app-wide block, so an over-permissive quote bucket
# takes order placement down with it.
#
# These are per Kite app. Verify against current docs before live use — they
# are not published as a machine-readable contract and have changed before.
KITE_LIMITS = {
    "quote": 1.0,
    "historical": 3.0,
    "order": 10.0,
    "default": 10.0,
}


class DailyQuotaExceeded(Exception):
    """Raised when a broker's daily call allowance is spent."""


class DailyQuota:
    """A per-day call counter, for brokers that cap calls per day as well as
    per second.

    Separate from the token bucket because it is a different kind of limit: a
    bucket that refills cannot express "5000 and then nothing until tomorrow",
    and silently spending the last of a daily allowance on a status poll would
    leave nothing for an order.
    """

    def __init__(self, redis: aioredis.Redis, *, name: str, limit: int):
        self._redis = redis
        self._name = name
        self._limit = limit

    def key(self, day: str) -> str:
        return f"quota:{self._name}:{day}"

    async def take(self, cost: int = 1, *, day: str | None = None) -> int:
        """Spend from today's allowance, or raise. Returns the remaining count."""
        from datetime import datetime

        from app.domain.calendar import IST

        stamp = day or datetime.now(IST).date().isoformat()
        key = self.key(stamp)
        used = await self._redis.incrby(key, cost)
        if used == cost:
            # First call of the day: expire a little past midnight so the key
            # cannot outlive the day it counts.
            await self._redis.expire(key, 26 * 3600)
        remaining = self._limit - used
        if remaining < 0:
            raise DailyQuotaExceeded(
                f"{self._name} has spent its {self._limit} calls for {stamp}. "
                "The allowance resets tomorrow."
            )
        return remaining

    async def remaining(self, *, day: str | None = None) -> int:
        from datetime import datetime

        from app.domain.calendar import IST

        stamp = day or datetime.now(IST).date().isoformat()
        used = await self._redis.get(self.key(stamp))
        return self._limit - int(used or 0)


# ICICI Breeze publishes tighter limits than Kite, and both axes bind: a
# per-minute rate and a per-day total. Verify against current docs before live
# use; exceeding them is documented as blocking the account rather than
# returning a retryable error.
BREEZE_CALLS_PER_MINUTE = 100
BREEZE_CALLS_PER_DAY = 5000


def breeze_limiter(redis: aioredis.Redis, *, session_key: str) -> RateLimiter:
    """Per-second pacing for Breeze, derived from its per-minute limit.

    Keyed by session rather than app: Breeze documents its limits per user,
    unlike Kite's per-application budget.
    """
    return RateLimiter(
        redis,
        name=f"breeze:{session_key}",
        rate_per_second=BREEZE_CALLS_PER_MINUTE / 60,
        # A minute's worth would let one burst spend the whole minute's
        # allowance in a second; a quarter keeps some in reserve for an order
        # that arrives while a sync is running.
        burst=max(1, BREEZE_CALLS_PER_MINUTE // 4),
    )


def breeze_quota(redis: aioredis.Redis, *, session_key: str) -> DailyQuota:
    return DailyQuota(
        redis, name=f"breeze:{session_key}", limit=BREEZE_CALLS_PER_DAY
    )


def kite_limiter(redis: aioredis.Redis, *, api_key: str, category: str) -> RateLimiter:
    """A limiter for one Kite app and endpoint category.

    Keyed by api_key because the limit belongs to the Kite application: two
    users of the same app share a budget, while separate apps do not.
    """
    rate = KITE_LIMITS.get(category, KITE_LIMITS["default"])
    return RateLimiter(
        redis,
        name=f"kite:{api_key}:{category}",
        rate_per_second=rate,
    )
