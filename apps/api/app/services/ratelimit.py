"""Redis fixed-window rate limiting for authentication endpoints.

Applied to login, password reset and (in 1b-ii) TOTP verification. TOTP codes
are six digits, so rate limiting is what makes a second factor meaningful
rather than a formality — an unlimited verify endpoint is brute-forceable in
minutes.

Fixed window rather than sliding: an attacker can get at most 2x the limit
across a window boundary, which is an acceptable trade for a counter that is a
single INCR and cannot drift.
"""

import redis.asyncio as aioredis

DEFAULT_LIMIT = 5
DEFAULT_WINDOW_SECONDS = 15 * 60


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Too many attempts; retry in {retry_after}s")


def _key(scope: str, identifier: str) -> str:
    return f"ratelimit:{scope}:{identifier.lower()}"


async def check(
    redis: aioredis.Redis,
    scope: str,
    identifier: str,
    *,
    limit: int = DEFAULT_LIMIT,
) -> None:
    """Raises RateLimitExceeded if this identifier is over the limit.

    Read-only: the counter advances in `record_failure`, so a successful
    request never consumes budget.
    """
    key = _key(scope, identifier)
    current = await redis.get(key)
    if current is not None and int(current) >= limit:
        ttl = await redis.ttl(key)
        raise RateLimitExceeded(retry_after=max(ttl, 1))


async def record_failure(
    redis: aioredis.Redis,
    scope: str,
    identifier: str,
    *,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> int:
    key = _key(scope, identifier)
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window_seconds)
    return count


async def clear(redis: aioredis.Redis, scope: str, identifier: str) -> None:
    """Called on success: a legitimate user who mistypes a password twice then
    gets it right should not carry those failures for the rest of the window."""
    await redis.delete(_key(scope, identifier))
