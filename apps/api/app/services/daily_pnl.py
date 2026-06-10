"""Daily realized P&L tracker.

Positions store *cumulative* realized P&L, so summing them cannot answer
"how much did this user lose today" — the MAX_DAILY_LOSS rule needs the
per-day delta. The paper engine pushes each fill's realized delta into a
per-day Redis counter (IST trading day); the risk engine reads it.

Caveat: the counter lives in Redis — a flush/restart without persistence
resets it to zero for the day. Conservative deployments should enable
Redis AOF. TODO(durability): mirror into a Postgres daily ledger once the
charges engine lands.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis

IST = ZoneInfo("Asia/Kolkata")
KEY_TTL_SECONDS = 3 * 24 * 3600


def _key(user_id: uuid.UUID | str, environment: str) -> str:
    day = datetime.now(IST).date().isoformat()
    return f"pnl:realized:{user_id}:{environment}:{day}"


async def add_realized(
    redis: aioredis.Redis, user_id: uuid.UUID, environment: str, delta: Decimal
) -> None:
    if delta == 0:
        return
    key = _key(user_id, environment)
    # INCRBYFLOAT precision is fine here: the value feeds a rupee-level
    # risk threshold, not accounting records (fills table stays exact).
    await redis.incrbyfloat(key, float(delta))
    await redis.expire(key, KEY_TTL_SECONDS)


async def get_realized(
    redis: aioredis.Redis, user_id: uuid.UUID, environment: str
) -> Decimal:
    raw = await redis.get(_key(user_id, environment))
    return Decimal(raw) if raw is not None else Decimal("0")
