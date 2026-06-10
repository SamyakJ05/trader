import uuid
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession
from app.core.redis import get_redis
from app.db.models import BrokerAccount, FundsSnapshot, HoldingsSnapshot, Position
from app.domain.enums import Environment
from app.engines.paper import market_sim
from app.services import brokers as broker_service

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("/funds")
async def funds(user: CurrentUser, db: DbSession, account_id: uuid.UUID):
    account = await broker_service.get_account(db, user.id, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    result = await db.execute(
        select(FundsSnapshot)
        .where(FundsSnapshot.broker_account_id == account_id)
        .order_by(FundsSnapshot.ts.desc())
        .limit(1)
    )
    snap = result.scalar_one_or_none()
    if snap is None:
        return {"available_cash": "1000000.00", "as_of": None, "note": "paper default, no snapshot yet"}
    return {"available_cash": str(snap.available_cash), "as_of": snap.ts.isoformat()}


@router.get("/positions")
async def positions(
    user: CurrentUser, db: DbSession, environment: Environment = Environment.PAPER
):
    result = await db.execute(
        select(Position).where(
            Position.user_id == user.id, Position.environment == environment.value
        )
    )
    redis = get_redis()
    out = []
    for p in result.scalars():
        last = (
            await market_sim.get_price(redis, p.symbol)
            if environment == Environment.PAPER
            else p.last_price or Decimal("0")
        )
        unrealized = (last - p.average_price) * p.quantity if p.quantity else Decimal("0")
        out.append(
            {
                "id": str(p.id),
                "broker_account_id": str(p.broker_account_id),
                "symbol": p.symbol,
                "exchange": p.exchange,
                "product": p.product,
                "quantity": p.quantity,
                "average_price": str(p.average_price),
                "last_price": str(last),
                "realized_pnl": str(p.realized_pnl),
                "unrealized_pnl": str(unrealized),
            }
        )
    return out


@router.get("/holdings")
async def holdings(user: CurrentUser, db: DbSession, account_id: uuid.UUID):
    account = await broker_service.get_account(db, user.id, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    result = await db.execute(
        select(HoldingsSnapshot)
        .where(HoldingsSnapshot.broker_account_id == account_id)
        .order_by(HoldingsSnapshot.ts.desc())
        .limit(1)
    )
    snap = result.scalar_one_or_none()
    if snap is None:
        return {"holdings": [], "as_of": None, "note": "no snapshot; run account sync"}
    return {"holdings": snap.holdings, "as_of": snap.ts.isoformat()}


@router.get("/summary")
async def summary(
    user: CurrentUser, db: DbSession, environment: Environment = Environment.PAPER
):
    accounts = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user.id,
            BrokerAccount.environment == environment.value,
        )
    )
    accounts = accounts.scalars().all()

    position_rows = await db.execute(
        select(Position).where(
            Position.user_id == user.id, Position.environment == environment.value
        )
    )
    redis = get_redis()
    realized = Decimal("0")
    unrealized = Decimal("0")
    open_positions = 0
    for p in position_rows.scalars():
        realized += p.realized_pnl or Decimal("0")
        if p.quantity:
            open_positions += 1
            last = (
                await market_sim.get_price(redis, p.symbol)
                if environment == Environment.PAPER
                else p.last_price or p.average_price
            )
            unrealized += (last - p.average_price) * p.quantity

    return {
        "environment": environment.value,
        "accounts": len(accounts),
        "open_positions": open_positions,
        "realized_pnl": str(realized),
        "unrealized_pnl": str(unrealized),
    }
