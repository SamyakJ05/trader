"""Daily realized P&L tracker.

Positions store *cumulative* realized P&L, so summing them cannot answer
"how much did this user lose today" — the MAX_DAILY_LOSS rule needs the
per-day delta. The paper engine pushes each fill's realized delta into a
per-day Redis counter (IST trading day); the risk engine reads it.

Durability: the figure gates real money, so it is written to Postgres as well
as Redis. Redis is the hot path — the risk engine reads it on every order —
but a flush or a restart without persistence would reset it to zero mid-day,
and the rule would go on evaluating and passing while protecting nothing.
Reads fall back to the database when the cache is cold, and repopulate it.

That also makes the deployment choice smaller: a managed Redis whose
durability guarantees are vague is fine here, because Redis is no longer the
system of record for anything that matters.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DailyPnl

IST = ZoneInfo("Asia/Kolkata")
KEY_TTL_SECONDS = 3 * 24 * 3600


def _key(user_id: uuid.UUID | str, environment: str) -> str:
    day = datetime.now(IST).date().isoformat()
    return f"pnl:realized:{user_id}:{environment}:{day}"


def trading_day(now: datetime | None = None) -> date:
    """The IST trading date a moment belongs to.

    A fill at 22:00 UTC is 03:30 the next morning in India, and belongs to that
    session rather than the one that closed hours earlier.
    """
    return (now or datetime.now(IST)).astimezone(IST).date()


async def add_realized(
    redis: aioredis.Redis,
    user_id: uuid.UUID,
    environment: str,
    delta: Decimal,
    db: AsyncSession | None = None,
) -> None:
    """Record a realized P&L delta for today.

    `db` is optional so existing callers keep working, but a caller with a
    session should pass it: without one the figure is only in Redis, and a
    flush takes the day's losses with it.
    """
    if delta == 0:
        return
    key = _key(user_id, environment)
    # INCRBYFLOAT precision is fine here: the value feeds a rupee-level
    # risk threshold, not accounting records (fills table stays exact).
    await redis.incrbyfloat(key, float(delta))
    await redis.expire(key, KEY_TTL_SECONDS)

    if db is None:
        return
    day = trading_day()
    # One statement, so concurrent fills cannot read-modify-write over each
    # other. The unique constraint on (user, environment, day) is what makes
    # the upsert land on a single row.
    await db.execute(
        insert(DailyPnl)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            environment=environment,
            trading_day=day,
            realized=delta,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "environment", "trading_day"],
            set_={"realized": DailyPnl.realized + delta, "updated_at": func.now()},
        )
    )


async def get_realized(
    redis: aioredis.Redis,
    user_id: uuid.UUID,
    environment: str,
    db: AsyncSession | None = None,
) -> Decimal:
    """Today's realized P&L, from Redis where possible.

    A cache miss is not the same as zero: it may be a flushed Redis on a day
    with losses. When a session is available the database settles it, and the
    cache is repopulated so the next read is hot again.
    """
    raw = await redis.get(_key(user_id, environment))
    if raw is not None:
        return Decimal(raw)
    if db is None:
        return Decimal("0")

    stored = (
        await db.execute(
            select(DailyPnl.realized).where(
                DailyPnl.user_id == user_id,
                DailyPnl.environment == environment,
                DailyPnl.trading_day == trading_day(),
            )
        )
    ).scalar_one_or_none()
    if stored is None:
        return Decimal("0")
    # Warm the cache so the risk engine is not querying Postgres per order.
    await redis.set(_key(user_id, environment), str(stored), ex=KEY_TTL_SECONDS)
    return Decimal(stored)
