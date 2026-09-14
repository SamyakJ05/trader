"""Kill switches. Redis-backed for a fast read on every order path;
every flip is audited. Global switch halts all order placement and
strategy execution; per-strategy switch halts one strategy."""

import uuid

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AuditEventType
from app.services import audit

GLOBAL_KEY = "kill:global"
STRATEGY_KEY = "kill:strategy:{strategy_id}"


async def is_global_engaged(redis: aioredis.Redis) -> bool:
    return await redis.exists(GLOBAL_KEY) == 1


async def is_strategy_engaged(redis: aioredis.Redis, strategy_id: uuid.UUID | str) -> bool:
    return await redis.exists(STRATEGY_KEY.format(strategy_id=strategy_id)) == 1


async def set_global(
    redis: aioredis.Redis,
    db: AsyncSession,
    *,
    engaged: bool,
    user_id: uuid.UUID,
    reason: str,
) -> None:
    if engaged:
        await redis.set(GLOBAL_KEY, reason or "engaged")
    else:
        await redis.delete(GLOBAL_KEY)
    await audit.emit(
        db,
        AuditEventType.KILL_SWITCH,
        user_id=user_id,
        entity_type="system",
        entity_id="global",
        payload={"engaged": engaged, "reason": reason},
    )


async def set_strategy(
    redis: aioredis.Redis,
    db: AsyncSession,
    *,
    strategy_id: uuid.UUID,
    engaged: bool,
    user_id: uuid.UUID,
    reason: str,
) -> None:
    key = STRATEGY_KEY.format(strategy_id=strategy_id)
    if engaged:
        await redis.set(key, reason or "engaged")
    else:
        await redis.delete(key)
    await audit.emit(
        db,
        AuditEventType.KILL_SWITCH,
        user_id=user_id,
        entity_type="strategy",
        entity_id=str(strategy_id),
        payload={"engaged": engaged, "reason": reason},
    )


async def status(
    redis: aioredis.Redis,
    owned_strategy_ids: set[uuid.UUID] | set[str] | None = None,
) -> dict:
    """Kill-switch status for one caller.

    `owned_strategy_ids` scopes `killed_strategies` to the caller's own
    strategies. The Redis keyspace is global, so an unfiltered scan would
    hand every tenant's strategy ids to any authenticated user — which is
    also exactly what an attacker needs to target a foreign kill switch.
    Pass an empty set for 'this user owns nothing'; None is accepted only
    for operator/system callers that legitimately see everything.
    """
    reason = await redis.get(GLOBAL_KEY)
    strategy_keys = [k async for k in redis.scan_iter("kill:strategy:*")]
    killed = [k.rsplit(":", 1)[-1] for k in strategy_keys]
    if owned_strategy_ids is not None:
        owned = {str(s) for s in owned_strategy_ids}
        killed = [k for k in killed if k in owned]
    return {
        "global_engaged": reason is not None,
        "global_reason": reason,
        "killed_strategies": killed,
    }
