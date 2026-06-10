import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession
from app.core.redis import get_redis
from app.db.models import Strategy, TradingSignal
from app.domain.enums import AuditEventType, Environment, StrategyStatus
from app.engines.strategy.runner import STRATEGY_REGISTRY
from app.services import audit, killswitch

router = APIRouter(prefix="/strategies", tags=["strategies"])


class StrategyBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    kind: str
    broker_account_id: uuid.UUID
    environment: Environment = Environment.PAPER
    symbols: list[str] = Field(min_length=1)
    params: dict = Field(default_factory=dict)


class StrategyOut(BaseModel):
    id: str
    name: str
    kind: str
    environment: str
    symbols: list
    params: dict
    status: str
    broker_account_id: str | None
    created_at: datetime
    killed: bool = False


def _out(s: Strategy, killed: bool = False) -> StrategyOut:
    return StrategyOut(
        id=str(s.id),
        name=s.name,
        kind=s.kind,
        environment=s.environment,
        symbols=s.symbols,
        params=s.params,
        status=s.status,
        broker_account_id=str(s.broker_account_id) if s.broker_account_id else None,
        created_at=s.created_at,
        killed=killed,
    )


@router.get("/kinds")
async def kinds():
    return {"kinds": list(STRATEGY_REGISTRY.keys())}


@router.get("", response_model=list[StrategyOut])
async def list_strategies(user: CurrentUser, db: DbSession):
    result = await db.execute(
        select(Strategy).where(Strategy.user_id == user.id).order_by(Strategy.created_at)
    )
    redis = get_redis()
    out = []
    for s in result.scalars():
        out.append(_out(s, killed=await killswitch.is_strategy_engaged(redis, s.id)))
    return out


@router.post("", response_model=StrategyOut, status_code=201)
async def create_strategy(body: StrategyBody, user: CurrentUser, db: DbSession):
    if body.kind not in STRATEGY_REGISTRY:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown strategy kind '{body.kind}'; available: {list(STRATEGY_REGISTRY)}",
        )
    strategy = Strategy(
        user_id=user.id,
        name=body.name,
        kind=body.kind,
        broker_account_id=body.broker_account_id,
        environment=body.environment.value,
        symbols=body.symbols,
        params=body.params,
        status=StrategyStatus.DRAFT.value,
    )
    db.add(strategy)
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=user.id,
        entity_type="strategy",
        payload={"action": "create", "name": body.name, "kind": body.kind},
    )
    await db.commit()
    return _out(strategy)


async def _owned(db: DbSession, user: CurrentUser, strategy_id: uuid.UUID) -> Strategy:
    result = await db.execute(
        select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == user.id)
    )
    strategy = result.scalar_one_or_none()
    if strategy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    return strategy


@router.post("/{strategy_id}/start", response_model=StrategyOut)
async def start(strategy_id: uuid.UUID, user: CurrentUser, db: DbSession):
    strategy = await _owned(db, user, strategy_id)
    if strategy.environment == Environment.LIVE.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Live strategy execution is disabled in this MVP build",
        )
    strategy.status = StrategyStatus.RUNNING.value
    await audit.emit(
        db, AuditEventType.USER_ACTION, user_id=user.id,
        entity_type="strategy", entity_id=strategy.id, payload={"action": "start"},
    )
    await db.commit()
    return _out(strategy)


@router.post("/{strategy_id}/stop", response_model=StrategyOut)
async def stop(strategy_id: uuid.UUID, user: CurrentUser, db: DbSession):
    strategy = await _owned(db, user, strategy_id)
    strategy.status = StrategyStatus.STOPPED.value
    await audit.emit(
        db, AuditEventType.USER_ACTION, user_id=user.id,
        entity_type="strategy", entity_id=strategy.id, payload={"action": "stop"},
    )
    await db.commit()
    return _out(strategy)


@router.delete("/{strategy_id}", status_code=204)
async def delete_strategy(strategy_id: uuid.UUID, user: CurrentUser, db: DbSession):
    strategy = await _owned(db, user, strategy_id)
    if strategy.status == StrategyStatus.RUNNING.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "Stop the strategy before deleting")
    await db.delete(strategy)
    await db.commit()


@router.get("/{strategy_id}/signals")
async def signals(strategy_id: uuid.UUID, user: CurrentUser, db: DbSession, limit: int = 50):
    await _owned(db, user, strategy_id)
    result = await db.execute(
        select(TradingSignal)
        .where(TradingSignal.strategy_id == strategy_id)
        .order_by(TradingSignal.ts.desc())
        .limit(min(limit, 200))
    )
    return [
        {
            "id": str(s.id),
            "ts": s.ts.isoformat(),
            "symbol": s.symbol,
            "signal_type": s.signal_type,
            "payload": s.payload,
            "acted": s.acted,
            "order_id": str(s.order_id) if s.order_id else None,
        }
        for s in result.scalars()
    ]
