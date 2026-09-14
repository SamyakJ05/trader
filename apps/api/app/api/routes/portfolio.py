import uuid
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.core.deps import DbSession, VerifiedUser
from app.core.redis import get_redis
from app.db.models import (
    BrokerAccount,
    FundsSnapshot,
    HoldingsSnapshot,
    Position,
    PaperHolding,
    CashLedger,
    PendingSettlement,
)
from app.domain.enums import Environment
from app.engines.paper import market_sim, ledger
from app.services import brokers as broker_service

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("/funds")
async def funds(user: VerifiedUser, db: DbSession, account_id: uuid.UUID):
    account = await broker_service.get_account(db, user.id, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    if account.environment == "paper":
        entry = await ledger.latest(db, account_id)
        return {
            "available_cash": str(await ledger.get_cash(db, account_id)),
            "as_of": entry.ts.isoformat() if entry else None,
            "source": "cash_ledger",
        }
    result = await db.execute(
        select(FundsSnapshot)
        .where(FundsSnapshot.broker_account_id == account_id)
        .order_by(FundsSnapshot.ts.desc())
        .limit(1)
    )
    snap = result.scalar_one_or_none()
    if snap is None:
        return {
            "available_cash": None,
            "as_of": None,
            "note": "No broker snapshot; sync the account",
        }
    return {"available_cash": str(snap.available_cash), "as_of": snap.ts.isoformat()}


@router.get("/positions")
async def positions(
    user: VerifiedUser, db: DbSession, environment: Environment = Environment.PAPER
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
async def holdings(user: VerifiedUser, db: DbSession, account_id: uuid.UUID):
    account = await broker_service.get_account(db, user.id, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    if account.environment == "paper":
        rows = (
            (
                await db.execute(
                    select(PaperHolding).where(
                        PaperHolding.broker_account_id == account_id,
                        PaperHolding.quantity > 0,
                    )
                )
            )
            .scalars()
            .all()
        )
        pending = (
            (
                await db.execute(
                    select(PendingSettlement).where(
                        PendingSettlement.broker_account_id == account_id,
                        PendingSettlement.settled_at.is_(None),
                        PendingSettlement.cancelled_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        return {
            "holdings": [
                dict(
                    symbol=r.symbol,
                    exchange=r.exchange,
                    quantity=r.quantity,
                    average_price=str(r.average_price),
                )
                for r in rows
            ],
            "pending": [
                dict(
                    symbol=r.symbol,
                    exchange=r.exchange,
                    quantity=r.quantity,
                    settles_on=r.settles_on.isoformat(),
                )
                for r in pending
            ],
            "as_of": None,
        }
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
async def summary(user: VerifiedUser, db: DbSession, environment: Environment = Environment.PAPER):
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

    if environment == Environment.PAPER:
        rows = (
            (
                await db.execute(
                    select(PaperHolding)
                    .join(BrokerAccount)
                    .where(
                        BrokerAccount.user_id == user.id,
                        PaperHolding.quantity > 0,
                    )
                )
            )
            .scalars()
            .all()
        )
        for holding in rows:
            last = await market_sim.get_price(redis, holding.symbol)
            unrealized += (last - holding.average_price) * holding.quantity

    return {
        "environment": environment.value,
        "accounts": len(accounts),
        "open_positions": open_positions,
        "realized_pnl": str(realized),
        "unrealized_pnl": str(unrealized),
    }


@router.get("/ledger")
async def cash_entries(
    user: VerifiedUser, db: DbSession, account_id: uuid.UUID, before_id: int | None = None
):
    account = await broker_service.get_account(db, user.id, account_id)
    if account is None:
        raise HTTPException(404, "Account not found")
    query = select(CashLedger).where(CashLedger.broker_account_id == account_id)
    if before_id is not None:
        query = query.where(CashLedger.id < before_id)
    rows = (await db.execute(query.order_by(CashLedger.id.desc()).limit(100))).scalars().all()
    return [
        dict(
            id=r.id,
            ts=r.ts,
            entry_type=r.entry_type,
            amount=str(r.amount),
            balance=str(r.balance),
            fill_id=str(r.fill_id) if r.fill_id else None,
        )
        for r in rows
    ]


@router.get("/delivery")
async def delivery_holdings(user: VerifiedUser, db: DbSession):
    rows = (
        (
            await db.execute(
                select(PaperHolding)
                .join(BrokerAccount)
                .where(
                    BrokerAccount.user_id == user.id,
                    BrokerAccount.environment == "paper",
                    PaperHolding.quantity > 0,
                )
                .order_by(PaperHolding.symbol, PaperHolding.broker_account_id)
            )
        )
        .scalars()
        .all()
    )
    redis = get_redis()
    out = []
    for row in rows:
        last = await market_sim.get_price(redis, row.symbol)
        out.append(
            dict(
                id=str(row.id),
                broker_account_id=str(row.broker_account_id),
                symbol=row.symbol,
                exchange=row.exchange,
                quantity=row.quantity,
                average_price=str(row.average_price),
                last_price=str(last),
                unrealized_pnl=str((last - row.average_price) * row.quantity),
            )
        )
    return out
