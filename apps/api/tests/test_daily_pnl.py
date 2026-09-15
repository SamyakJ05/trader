"""Durable daily realized P&L.

This figure is what MAX_DAILY_LOSS compares against. When it lived only in
Redis, a flush reset it to zero mid-day and the rule went on evaluating and
passing — protecting nothing, while logging that all checks passed. These
tests exist for that failure.

The Postgres path needs a real database; the cache-behaviour tests do not.
"""

import os
import uuid
from decimal import Decimal

import fakeredis.aioredis
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import daily_pnl


def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


# ── cache behaviour, no database needed ──────────────────────────────


async def test_a_cold_cache_without_a_database_reads_zero():
    """Callers that pass no session get the old behaviour. Documented rather
    than relied upon: it is why the session is threaded through."""
    assert await daily_pnl.get_realized(redis(), uuid.uuid4(), "live") == Decimal("0")


async def test_redis_accumulates_within_a_day():
    r = redis()
    user = uuid.uuid4()
    await daily_pnl.add_realized(r, user, "live", Decimal("-100"))
    await daily_pnl.add_realized(r, user, "live", Decimal("-50"))
    assert await daily_pnl.get_realized(r, user, "live") == Decimal("-150")


async def test_environments_do_not_share_a_counter():
    """A paper loss must not trip a live account's limit, or the reverse."""
    r = redis()
    user = uuid.uuid4()
    await daily_pnl.add_realized(r, user, "paper", Decimal("-5000"))
    assert await daily_pnl.get_realized(r, user, "live") == Decimal("0")


def test_the_trading_day_is_ist_not_utc():
    """A fill at 22:00 UTC is 03:30 next morning in India and belongs to that
    session — a UTC day would file it against the one that already closed."""
    from datetime import datetime, timezone

    late_utc = datetime(2026, 9, 15, 22, 0, tzinfo=timezone.utc)
    assert daily_pnl.trading_day(late_utc).isoformat() == "2026-09-16"


# ── durability, needs Postgres ───────────────────────────────────────

pg = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="requires disposable Postgres"
)


@pytest.fixture
async def db():
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()
    await engine.dispose()


@pytest.fixture
async def user(db):
    from app.db.models import User

    row = User(
        email=f"pnl-{uuid.uuid4().hex[:8]}@x.com", password_hash="x", is_active=True
    )
    db.add(row)
    await db.commit()
    return row


@pg
async def test_a_redis_flush_does_not_lose_the_days_losses(db, user):
    """The failure this exists for: Redis restarts, the counter reads zero,
    and a rule that should have halted trading keeps passing."""
    r = redis()
    await daily_pnl.add_realized(r, user.id, "live", Decimal("-5000"), db)
    await db.commit()

    await r.flushall()  # the event

    recovered = await daily_pnl.get_realized(r, user.id, "live", db)
    assert recovered == Decimal("-5000")


@pg
async def test_recovery_warms_the_cache(db, user):
    """The risk engine reads this on every order; it should not query
    Postgres each time after one cold read."""
    r = redis()
    await daily_pnl.add_realized(r, user.id, "live", Decimal("-250"), db)
    await db.commit()
    await r.flushall()

    await daily_pnl.get_realized(r, user.id, "live", db)
    assert await r.get(daily_pnl._key(user.id, "live")) is not None


@pg
async def test_concurrent_fills_do_not_overwrite_each_other(db, user):
    """The upsert adds to the stored value in one statement. A read-modify-
    write would let two fills landing together lose one of the losses."""
    for _ in range(5):
        await daily_pnl.add_realized(r_ := redis(), user.id, "live", Decimal("-10"), db)
    await db.commit()

    fresh = redis()
    assert await daily_pnl.get_realized(fresh, user.id, "live", db) == Decimal("-50")


@pg
async def test_the_ledger_is_per_day(db, user):
    """Yesterday's losses must not count against today's limit."""
    from app.db.models import DailyPnl
    from datetime import date

    db.add(
        DailyPnl(
            id=uuid.uuid4(), user_id=user.id, environment="live",
            trading_day=date(2020, 1, 1), realized=Decimal("-99999"),
        )
    )
    await db.commit()

    assert await daily_pnl.get_realized(redis(), user.id, "live", db) == Decimal("0")
