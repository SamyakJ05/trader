"""Worker liveness.

The worker's silent death is the failure that keeps the api up while positions
stop moving, so these pin the two properties that make it detectable: the beat
expires on its own, and the beat is stamped before anything that can throw.
"""

import pytest
from fakeredis import FakeAsyncRedis

from app.services import heartbeat


@pytest.fixture
def redis():
    return FakeAsyncRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_no_beat_means_not_alive(redis):
    """A worker that has never run reads as dead, not as unknown."""
    assert await heartbeat.last_beat(redis) is None
    assert await heartbeat.is_alive(redis) is False


@pytest.mark.asyncio
async def test_beat_marks_alive(redis):
    await heartbeat.beat(redis)
    assert await heartbeat.is_alive(redis) is True
    assert isinstance(await heartbeat.last_beat(redis), int)


@pytest.mark.asyncio
async def test_beat_carries_a_ttl(redis):
    """Expiry is the whole mechanism: nothing polls the worker, so the key
    lapsing on its own is what surfaces a death."""
    await heartbeat.beat(redis)
    ttl = await redis.ttl(heartbeat.HEARTBEAT_KEY)
    assert 0 < ttl <= heartbeat.HEARTBEAT_TTL_SECONDS


@pytest.mark.asyncio
async def test_expiry_reads_as_dead(redis):
    await heartbeat.beat(redis)
    await redis.delete(heartbeat.HEARTBEAT_KEY)  # stands in for the TTL lapsing
    assert await heartbeat.is_alive(redis) is False


@pytest.mark.asyncio
async def test_ttl_outlives_several_ticks():
    """Set too tight, a busy tick would report a healthy worker as dead. The
    TTL has to cover more than one tick interval to mean anything."""
    assert heartbeat.HEARTBEAT_TTL_SECONDS >= 15


@pytest.mark.asyncio
async def test_corrupt_value_reads_as_dead(redis):
    await redis.set(heartbeat.HEARTBEAT_KEY, "not-a-timestamp")
    assert await heartbeat.last_beat(redis) is None


@pytest.mark.asyncio
async def test_beat_survives_a_failing_stage(monkeypatch):
    """The beat answers "is the loop running", not "did every stage succeed".

    A persistent failure in settlement or fills is a bug to fix; reporting the
    worker as dead for it would send an operator after the wrong thing, and
    would mask a real death behind an already-red signal.
    """
    from app.workers import jobs

    fake = FakeAsyncRedis(decode_responses=True)
    monkeypatch.setattr(jobs, "get_redis", lambda: fake)

    async def boom(*args, **kwargs):
        raise RuntimeError("price feed down")

    monkeypatch.setattr(jobs.market_sim, "tick_all", boom)

    settled = []

    async def record_settlement(db):
        settled.append(True)

    monkeypatch.setattr(jobs, "settle_due", record_settlement)

    await jobs.paper_tick({})

    # Stamped before the stage that blew up.
    assert await heartbeat.is_alive(fake) is True
    # And the price stage failing did not take settlement with it: T+1
    # holdings becoming available does not depend on a price feed.
    assert settled == [True]
