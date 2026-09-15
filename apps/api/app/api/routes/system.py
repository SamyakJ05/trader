import time
import uuid

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.deps import DbSession, VerifiedUser
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.db.models import Strategy
from app.domain.enums import Broker, StrategyStatus
from app.engines.paper import engine as paper_engine
from app.services import brokers as broker_service
from app.services import heartbeat, killswitch

logger = get_logger(__name__)

router = APIRouter(tags=["system"])


@router.get("/healthz")
async def healthz(db: DbSession, response: Response):
    """Liveness of this process and its dependencies.

    Returns 503 when degraded, not 200 with a body saying so: uptime monitors
    and container orchestrators read the status code, and a 200 here would
    report the instance healthy while Postgres was unreachable.
    """
    checks = {"db": False, "redis": False}
    try:
        await db.execute(text("SELECT 1"))
        checks["db"] = True
    except Exception:
        logger.warning("healthz_db_unreachable", exc_info=True)
    try:
        checks["redis"] = await get_redis().ping()
    except Exception:
        logger.warning("healthz_redis_unreachable", exc_info=True)
    healthy = all(checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "degraded", "checks": checks}


@router.get("/readyz")
async def readyz(db: DbSession, response: Response):
    """Everything healthz covers, plus whether the worker is still ticking.

    The arq worker advances paper fills and runs strategies. When it dies the
    api stays up and every page keeps rendering -- positions simply stop
    moving. That is the failure least likely to be noticed, so it gets an
    endpoint an uptime monitor can watch.

    Kept separate from /healthz because the two answer different questions: a
    container orchestrator restarting the api because the worker died would be
    the wrong response to the right signal.
    """
    redis = get_redis()
    checks = {"db": False, "redis": False, "worker": False}
    try:
        await db.execute(text("SELECT 1"))
        checks["db"] = True
    except Exception:
        logger.warning("readyz_db_unreachable", exc_info=True)
    try:
        checks["redis"] = await redis.ping()
    except Exception:
        logger.warning("readyz_redis_unreachable", exc_info=True)

    last = None
    if checks["redis"]:
        # Redis expiring the key is what makes the worker's death visible, so
        # this check is only meaningful when Redis itself is reachable.
        try:
            last = await heartbeat.last_beat(redis)
            checks["worker"] = last is not None
        except Exception:
            logger.warning("readyz_heartbeat_failed", exc_info=True)

    ready = all(checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    body = {"status": "ok" if ready else "degraded", "checks": checks}
    if last is not None:
        body["worker_last_beat_age_seconds"] = max(0, int(time.time()) - last)
    return body


@router.get("/system/config")
async def system_config(user: VerifiedUser):
    settings = get_settings()
    return {
        "live_trading_enabled": settings.enable_live_trading,
        "market_hours_enforced": settings.market_hours_enforced,
    }


class KillSwitchBody(BaseModel):
    scope: str  # "global" | "strategy"
    engaged: bool
    strategy_id: uuid.UUID | None = None
    reason: str = ""


async def _owned_strategy_ids(db: DbSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    result = await db.execute(select(Strategy.id).where(Strategy.user_id == user_id))
    return set(result.scalars())


@router.get("/system/killswitch")
async def killswitch_status(user: VerifiedUser, db: DbSession):
    return await killswitch.status(
        get_redis(), owned_strategy_ids=await _owned_strategy_ids(db, user.id)
    )


@router.post("/system/killswitch")
async def set_killswitch(body: KillSwitchBody, user: VerifiedUser, db: DbSession):
    redis = get_redis()
    if body.scope == "global":
        # The global switch halts strategy execution for EVERY user on this
        # instance — an operator break-glass, not a per-user control.
        if not user.is_admin:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "The global kill switch is an operator action; "
                "use a per-strategy kill switch to stop your own strategies",
            )
        await killswitch.set_global(
            redis, db, engaged=body.engaged, user_id=user.id, reason=body.reason
        )
        if body.engaged:
            # Engaging globally stops running strategies hard, for every user
            # -- not only the operator's own. The Redis flag already halts new
            # orders instance-wide, so scoping this to the engager left every
            # other user's strategies recorded as RUNNING while they were not,
            # and resumed all of them at once on disengage.
            from sqlalchemy import update

            await db.execute(
                update(Strategy)
                .where(Strategy.status == StrategyStatus.RUNNING.value)
                .values(status=StrategyStatus.KILLED.value)
            )
    elif body.scope == "strategy":
        if body.strategy_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "strategy_id required")
        # Ownership is resolved BEFORE the switch is written: engaging a kill
        # switch on a foreign strategy halts it on every runner tick.
        result = await db.execute(
            select(Strategy).where(
                Strategy.id == body.strategy_id, Strategy.user_id == user.id
            )
        )
        strategy = result.scalar_one_or_none()
        if strategy is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
        await killswitch.set_strategy(
            redis,
            db,
            strategy_id=body.strategy_id,
            engaged=body.engaged,
            user_id=user.id,
            reason=body.reason,
        )
        if body.engaged and strategy.status == StrategyStatus.RUNNING.value:
            strategy.status = StrategyStatus.KILLED.value
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "scope must be global|strategy")
    await db.commit()
    return await killswitch.status(
        redis, owned_strategy_ids=await _owned_strategy_ids(db, user.id)
    )


@router.post("/paper/accounts/{account_id}/reset")
async def reset_paper_account(account_id: uuid.UUID, user: VerifiedUser, db: DbSession):
    account = await broker_service.get_account(db, user.id, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    # broker, not environment. reset_account deletes every Position and
    # PaperHolding row for the account and rewrites its cash ledger -- fine
    # for the simulator, destructive and irreversible for a real broker
    # account, which sits in paper environment for the whole of the
    # verification playbook and would have been accepted here.
    if account.broker != Broker.PAPER.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only the paper simulator can be reset — this is a real broker account",
        )
    await paper_engine.reset_account(db, account)
    await db.commit()
    return {"status": "reset", "cash": str(paper_engine.INITIAL_PAPER_CASH)}
