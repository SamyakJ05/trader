import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.deps import DbSession, VerifiedUser
from app.core.redis import get_redis
from app.db.models import Strategy, TradingSignal
from app.domain.enums import AuditEventType, Environment, StrategyStatus
from app.engines.strategy.runner import STRATEGY_REGISTRY
from app.services import audit, killswitch
from app.services import brokers as broker_service
from app.services import instruments as instrument_service

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
async def list_strategies(user: VerifiedUser, db: DbSession):
    result = await db.execute(
        select(Strategy).where(Strategy.user_id == user.id).order_by(Strategy.created_at)
    )
    redis = get_redis()
    out = []
    for s in result.scalars():
        out.append(_out(s, killed=await killswitch.is_strategy_engaged(redis, s.id)))
    return out


@router.post("", response_model=StrategyOut, status_code=201)
async def create_strategy(body: StrategyBody, user: VerifiedUser, db: DbSession):
    if body.kind not in STRATEGY_REGISTRY:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown strategy kind '{body.kind}'; available: {list(STRATEGY_REGISTRY)}",
        )
    # The runner loads this account verbatim on every tick and places orders
    # through it — an unvalidated id here is an order path into another
    # user's account.
    account = await broker_service.get_account(db, user.id, body.broker_account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Broker account not found")

    # Symbols are stored as each broker names them, and brokers disagree:
    # Breeze calls RELIANCE something like RELIND. A strategy naming a symbol
    # its broker does not recognise would find no candles, emit no signals, and
    # look merely quiet — so it is refused at creation, while the person
    # writing it is here to fix it.
    unknown = await instrument_service.unknown_symbols(
        db,
        broker=account.broker,
        symbols=body.symbols,
        exchange=body.params.get("exchange", "NSE"),
    )
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{account.broker} does not recognise: {', '.join(unknown)}. "
            f"Symbols are stored as each broker names them; sync the "
            f"instrument master for this account, then use the broker's own codes.",
        )

    strategy = Strategy(
        user_id=user.id,
        name=body.name,
        kind=body.kind,
        # Use the verified row's id, not the request body's.
        broker_account_id=account.id,
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


async def _owned(db: DbSession, user: VerifiedUser, strategy_id: uuid.UUID) -> Strategy:
    result = await db.execute(
        select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == user.id)
    )
    strategy = result.scalar_one_or_none()
    if strategy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    return strategy


@router.post("/{strategy_id}/start", response_model=StrategyOut)
async def start(strategy_id: uuid.UUID, user: VerifiedUser, db: DbSession):
    strategy = await _owned(db, user, strategy_id)
    if strategy.environment == Environment.LIVE.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Live strategy execution is disabled in this MVP build",
        )
    if strategy.kind == "ai_agent":
        from app.services.ai.llm import resolve_llm

        if await resolve_llm(db, user.id) is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "AI is not configured — set a provider on the AI Trading page first",
            )
    strategy.status = StrategyStatus.RUNNING.value
    await audit.emit(
        db, AuditEventType.USER_ACTION, user_id=user.id,
        entity_type="strategy", entity_id=strategy.id, payload={"action": "start"},
    )
    await db.commit()
    return _out(strategy)


@router.post("/{strategy_id}/stop", response_model=StrategyOut)
async def stop(strategy_id: uuid.UUID, user: VerifiedUser, db: DbSession):
    strategy = await _owned(db, user, strategy_id)
    strategy.status = StrategyStatus.STOPPED.value
    await audit.emit(
        db, AuditEventType.USER_ACTION, user_id=user.id,
        entity_type="strategy", entity_id=strategy.id, payload={"action": "stop"},
    )
    await db.commit()
    return _out(strategy)


@router.delete("/{strategy_id}", status_code=204)
async def delete_strategy(strategy_id: uuid.UUID, user: VerifiedUser, db: DbSession):
    strategy = await _owned(db, user, strategy_id)
    if strategy.status == StrategyStatus.RUNNING.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "Stop the strategy before deleting")
    await db.delete(strategy)
    await db.commit()


@router.get("/{strategy_id}/signals")
async def signals(strategy_id: uuid.UUID, user: VerifiedUser, db: DbSession, limit: int = 50):
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
