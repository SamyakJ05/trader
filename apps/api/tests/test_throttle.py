"""Client-side rate limiting.

Broker limits are per application and shared across every process, so this is
a Redis token bucket rather than an in-process one. The tests that matter are
the ones about shared state and atomicity — an in-process limiter would pass
the arithmetic tests and still be wrong.
"""

import asyncio
import time

import fakeredis.aioredis
import pytest

from app.adapters.throttle import (
    KITE_LIMITS,
    RateLimiter,
    ThrottleTimeout,
    kite_limiter,
)


def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def limiter(r=None, **kwargs):
    defaults = dict(name="test", rate_per_second=10.0)
    defaults.update(kwargs)
    return RateLimiter(r or redis(), **defaults)


# ── construction ─────────────────────────────────────────────────────


def test_a_non_positive_rate_is_refused():
    """A zero rate would mean every caller waits forever; negative is
    nonsense. Better to fail at construction than at the first call."""
    with pytest.raises(ValueError):
        limiter(rate_per_second=0)
    with pytest.raises(ValueError):
        limiter(rate_per_second=-1)


def test_burst_defaults_to_one_second_of_capacity():
    """Enough to absorb jitter without letting an idle caller dump a long
    backlog in one go."""
    assert RateLimiter(redis(), name="x", rate_per_second=3)._burst == 3
    assert RateLimiter(redis(), name="x", rate_per_second=0.5)._burst == 1


# ── taking tokens ────────────────────────────────────────────────────


async def test_a_fresh_bucket_allows_an_immediate_call():
    await limiter().acquire()  # must not raise or block


async def test_a_burst_is_allowed_then_the_rate_applies():
    """An idle caller may burst up to the bucket size; the next call has to
    wait for a refill."""
    lim = limiter(rate_per_second=5, burst=3)
    for _ in range(3):
        assert await lim._try_take(1) == 0

    wait = await lim._try_take(1)
    assert wait > 0, "the fourth call in a 3-token bucket must wait"
    assert wait <= 1 / 5 + 0.01


async def test_tokens_refill_over_time():
    lim = limiter(rate_per_second=100, burst=1)
    assert await lim._try_take(1) == 0
    assert await lim._try_take(1) > 0
    await asyncio.sleep(0.05)  # 100/s refills one token in 10ms
    assert await lim._try_take(1) == 0


async def test_acquire_waits_rather_than_failing():
    """A call that is merely early should be delayed, not lost."""
    lim = limiter(rate_per_second=50, burst=1)
    await lim.acquire()
    started = time.monotonic()
    await lim.acquire()
    assert time.monotonic() - started >= 0.015, "second call should have waited"


async def test_acquire_gives_up_past_its_budget():
    """A wedged bucket must surface as an error, not a request that hangs."""
    lim = limiter(rate_per_second=0.01, burst=1)
    await lim.acquire()
    with pytest.raises(ThrottleTimeout):
        await lim.acquire(timeout=0.1)


async def test_cost_can_exceed_one_token():
    lim = limiter(rate_per_second=10, burst=5)
    assert await lim._try_take(5) == 0
    assert await lim._try_take(1) > 0


# ── shared across processes ──────────────────────────────────────────


async def test_two_limiters_on_one_redis_share_a_budget():
    """The point of putting this in Redis: the API and every worker draw on
    one bucket, because the broker's limit is per application."""
    shared = redis()
    a = RateLimiter(shared, name="kite:app", rate_per_second=5, burst=2)
    b = RateLimiter(shared, name="kite:app", rate_per_second=5, burst=2)

    assert await a._try_take(1) == 0
    assert await b._try_take(1) == 0
    # The bucket is now empty for both, not just for one of them.
    assert await a._try_take(1) > 0
    assert await b._try_take(1) > 0


async def test_different_names_do_not_share_a_budget():
    shared = redis()
    a = RateLimiter(shared, name="kite:app-one", rate_per_second=1, burst=1)
    b = RateLimiter(shared, name="kite:app-two", rate_per_second=1, burst=1)
    assert await a._try_take(1) == 0
    assert await b._try_take(1) == 0, "a separate app has its own budget"


async def test_concurrent_takes_do_not_oversubscribe():
    """Read-refill-take in separate round trips would let two callers both see
    the last token and both take it. The Lua script makes it atomic."""
    lim = limiter(rate_per_second=1000, burst=5)
    results = await asyncio.gather(*(lim._try_take(1) for _ in range(10)))
    granted = sum(1 for wait in results if wait == 0)
    assert granted == 5, f"exactly the burst should be granted, got {granted}"


# ── Kite configuration ───────────────────────────────────────────────


def test_kite_categories_have_their_own_rates():
    """Quotes and orders are allowed more than general endpoints; sharing one
    conservative bucket would throttle them needlessly."""
    r = redis()
    quote = kite_limiter(r, api_key="abc", category="quote")
    other = kite_limiter(r, api_key="abc", category="anything-else")
    assert quote._rate == KITE_LIMITS["quote"]
    assert other._rate == KITE_LIMITS["default"]
    assert quote.key != other.key


def test_kite_buckets_are_keyed_by_api_key():
    """The limit belongs to the Kite application, so two users of one app
    share a budget and two apps do not."""
    r = redis()
    assert (
        kite_limiter(r, api_key="app-one", category="quote").key
        != kite_limiter(r, api_key="app-two", category="quote").key
    )


async def test_an_idle_bucket_expires():
    """An untouched bucket holds no useful state; leaving keys behind for every
    api key and category would accumulate."""
    r = redis()
    lim = kite_limiter(r, api_key="abc", category="quote")
    await lim.acquire()
    assert await r.ttl(lim.key) > 0


# ── Breeze: a per-minute rate and a per-day cap ──────────────────────
# Breeze binds on both axes, and exceeding its limits is documented as
# blocking the account rather than returning something retryable.


async def test_breeze_paces_below_its_per_minute_limit():
    from app.adapters.throttle import BREEZE_CALLS_PER_MINUTE, breeze_limiter

    lim = breeze_limiter(redis(), session_key="sess")
    assert lim._rate == BREEZE_CALLS_PER_MINUTE / 60


async def test_breeze_burst_keeps_some_of_the_minute_in_reserve():
    """A full minute's burst would let one sync spend the whole allowance in a
    second, leaving nothing for an order arriving behind it."""
    from app.adapters.throttle import BREEZE_CALLS_PER_MINUTE, breeze_limiter

    lim = breeze_limiter(redis(), session_key="sess")
    assert lim._burst < BREEZE_CALLS_PER_MINUTE


async def test_breeze_limits_are_per_session_not_per_app():
    """Breeze documents its limits per user, unlike Kite's per-application
    budget — sharing one bucket across users would throttle them needlessly."""
    from app.adapters.throttle import breeze_limiter

    r = redis()
    assert (
        breeze_limiter(r, session_key="user-one").key
        != breeze_limiter(r, session_key="user-two").key
    )


async def test_the_daily_quota_counts_down():
    from app.adapters.throttle import DailyQuota

    quota = DailyQuota(redis(), name="test", limit=10)
    assert await quota.take() == 9
    assert await quota.take(3) == 6
    assert await quota.remaining() == 6


async def test_the_daily_quota_refuses_once_spent():
    """A bucket that refills cannot express 'this many and then nothing until
    tomorrow', which is why this is a separate mechanism."""
    from app.adapters.throttle import DailyQuota, DailyQuotaExceeded

    quota = DailyQuota(redis(), name="test", limit=3)
    await quota.take(3)
    with pytest.raises(DailyQuotaExceeded):
        await quota.take()


async def test_the_daily_quota_is_per_day():
    from app.adapters.throttle import DailyQuota

    r = redis()
    quota = DailyQuota(r, name="test", limit=5)
    await quota.take(5, day="2026-09-15")
    assert await quota.remaining(day="2026-09-16") == 5


async def test_the_daily_quota_key_cannot_outlive_its_day():
    """A counter left behind would silently eat into the next day's
    allowance."""
    from app.adapters.throttle import DailyQuota

    r = redis()
    quota = DailyQuota(r, name="test", limit=5)
    await quota.take(day="2026-09-15")
    assert 0 < await r.ttl(quota.key("2026-09-15")) <= 26 * 3600


async def test_the_quota_message_says_when_it_resets():
    """An operator seeing this needs to know whether to wait minutes or hours."""
    from app.adapters.throttle import DailyQuota, DailyQuotaExceeded

    quota = DailyQuota(redis(), name="test", limit=1)
    await quota.take()
    with pytest.raises(DailyQuotaExceeded, match="resets tomorrow"):
        await quota.take()
