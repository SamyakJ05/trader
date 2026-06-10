import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text

from app.core.config import get_settings
from app.core.deps import CurrentUser, DbSession
from app.core.redis import get_redis
from app.db.models import Strategy
from app.domain.enums import StrategyStatus
from app.services import brokers as broker_service
from app.services import killswitch
from app.engines.paper import engine as paper_engine

router = APIRouter(tags=["system"])


@router.get("/healthz")
async def healthz(db: DbSession):
    checks = {"db": False, "redis": False}
    try:
        await db.execute(text("SELECT 1"))
        checks["db"] = True
    except Exception:
        pass
    try:
        checks["redis"] = await get_redis().ping()
    except Exception:
        pass
    healthy = all(checks.values())
    return {"status": "ok" if healthy else "degraded", "checks": checks}


@router.get("/system/config")
async def system_config(user: CurrentUser):
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


@router.get("/system/killswitch")
async def killswitch_status(user: CurrentUser):
    return await killswitch.status(get_redis())


@router.post("/system/killswitch")
async def set_killswitch(body: KillSwitchBody, user: CurrentUser, db: DbSession):
    redis = get_redis()
    if body.scope == "global":
        await killswitch.set_global(
            redis, db, engaged=body.engaged, user_id=user.id, reason=body.reason
        )
        if body.engaged:
            # Engaging globally also stops running strategies hard.
            from sqlalchemy import update

            await db.execute(
                update(Strategy)
                .where(
                    Strategy.user_id == user.id,
                    Strategy.status == StrategyStatus.RUNNING.value,
                )
                .values(status=StrategyStatus.KILLED.value)
            )
    elif body.scope == "strategy":
        if body.strategy_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "strategy_id required")
        await killswitch.set_strategy(
            redis,
            db,
            strategy_id=body.strategy_id,
            engaged=body.engaged,
            user_id=user.id,
            reason=body.reason,
        )
        strategy = await db.get(Strategy, body.strategy_id)
        if strategy and body.engaged and strategy.status == StrategyStatus.RUNNING.value:
            strategy.status = StrategyStatus.KILLED.value
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "scope must be global|strategy")
    await db.commit()
    return await killswitch.status(redis)


@router.post("/paper/accounts/{account_id}/reset")
async def reset_paper_account(account_id: uuid.UUID, user: CurrentUser, db: DbSession):
    account = await broker_service.get_account(db, user.id, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    if account.environment != "paper":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only paper accounts can be reset")
    await paper_engine.reset_account(db, account)
    await db.commit()
    return {"status": "reset", "cash": str(paper_engine.INITIAL_PAPER_CASH)}
