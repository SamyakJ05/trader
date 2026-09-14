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
# KEYS[1] bucket, ARGV: rate per second, burst, now (seconds, float), cost.
_TAKE = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

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
            args=[self._rate, self._burst, time.time(), cost],
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


# Kite's documented limits. Quotes are allowed a higher rate than everything
# else, and order placement is stricter still, so they get their own buckets
# rather than sharing one conservative number.
#
# These are per Kite app. Verify against current docs before live use — they
# are not published as a machine-readable contract and have changed before.
KITE_LIMITS = {
    "quote": 10.0,
    "order": 10.0,
    "default": 3.0,
}


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
