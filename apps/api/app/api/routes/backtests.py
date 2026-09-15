import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.core.deps import DbSession, VerifiedUser
from app.domain.enums import Broker
from app.db.models import BacktestRun, Candle
from app.domain.calendar import CalendarUnavailable
from app.engines.backtest import run_backtest
from app.engines.market.candles import bucket_start
from app.engines.strategy.runner import STRATEGY_REGISTRY

router = APIRouter(prefix="/backtests", tags=["backtests"])
MAX_BARS = 100000


class BacktestBody(BaseModel):
    kind: Literal["sma_crossover"] = "sma_crossover"
    symbol: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9&._-]+$")
    exchange: Literal["NSE"] = "NSE"
    product: Literal["MIS", "CNC"] = "MIS"
    interval: Literal["1m", "5m", "1d"] = "1d"
    source: Literal["simulator", "yfinance_unadjusted"] = "yfinance_unadjusted"
    start: datetime
    end: datetime
    initial_cash: Decimal = Field(default=Decimal("1000000"), gt=0, le=Decimal("1000000000"))
    fast: int = Field(default=5, ge=1, le=1000)
    slow: int = Field(default=20, ge=2, le=2000)
    quantity: int = Field(default=1, ge=1, le=100000)
    # Whose brokerage the simulated fills pay. Defaulting to paper charges
    # statutory costs but no broker's cut, which flatters a strategy meant for
    # a real account: ICICI's percentage brokerage alone is Rs 440 on a Rs 1
    # lakh delivery round trip, against Zerodha's nothing.
    broker: Literal["paper", "zerodha", "icici_breeze", "groww"] = "paper"

    @model_validator(mode="after")
    def validate_range(self):
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Start and end require timezone")
        if self.start >= self.end:
            raise ValueError("Start must precede end")
        if self.fast >= self.slow:
            raise ValueError("Fast window must be smaller than slow window")
        return self


def out(row):
    return dict(
        id=str(row.id),
        created_at=row.created_at,
        kind=row.kind,
        config=row.config,
        results=row.results,
    )


@router.post("", status_code=201)
async def create_backtest(body: BacktestBody, user: VerifiedUser, db: DbSession):
    cutoff = min(body.end, bucket_start(datetime.now(timezone.utc), body.interval))
    candles = (
        (
            await db.execute(
                select(Candle)
                .where(
                    Candle.symbol == body.symbol,
                    Candle.exchange == body.exchange,
                    Candle.interval == body.interval,
                    Candle.source == body.source,
                    Candle.ts >= body.start,
                    Candle.ts < cutoff,
                )
                .order_by(Candle.ts)
                .limit(MAX_BARS + 1)
            )
        )
        .scalars()
        .all()
    )
    if len(candles) > MAX_BARS:
        raise HTTPException(422, "Range exceeds 100,000 bars; select a shorter range")
    if len(candles) < body.slow + 2:
        raise HTTPException(422, "Not enough completed candles for warmup and a next-open fill")
    try:
        result = await run_in_threadpool(
            run_backtest,
            STRATEGY_REGISTRY[body.kind],
            candles,
            symbol=body.symbol,
            exchange=body.exchange,
            product=body.product,
            initial_cash=body.initial_cash,
            params=dict(fast=body.fast, slow=body.slow, quantity=body.quantity),
            # Metrics annualise by sqrt(periods per year), so the bar size has
            # to reach them: scoring minute bars as daily understates Sharpe by
            # roughly twenty times.
            interval=body.interval,
            broker=Broker(body.broker),
        )
    except (ValueError, CalendarUnavailable) as exc:
        raise HTTPException(422, str(exc)) from exc
    digest = hashlib.sha256(
        json.dumps(
            [
                [c.ts.isoformat(), str(c.open), str(c.high), str(c.low), str(c.close), c.volume]
                for c in candles
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    result.update(
        data_sha256=digest,
        candle_count=len(candles),
        source=body.source,
        first_candle=candles[0].ts.isoformat(),
        last_candle=candles[-1].ts.isoformat(),
    )
    row = BacktestRun(
        user_id=user.id, kind=body.kind, config=body.model_dump(mode="json"), results=result
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return out(row)


@router.get("")
async def list_backtests(user: VerifiedUser, db: DbSession):
    rows = (
        (
            await db.execute(
                select(BacktestRun)
                .where(BacktestRun.user_id == user.id)
                .order_by(BacktestRun.created_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    return [
        dict(
            id=str(r.id),
            created_at=r.created_at,
            kind=r.kind,
            config=r.config,
            total_return=r.results["total_return"],
        )
        for r in rows
    ]


@router.get("/{run_id}")
async def get_backtest(run_id: uuid.UUID, user: VerifiedUser, db: DbSession):
    row = (
        await db.execute(
            select(BacktestRun).where(
                BacktestRun.id == run_id,
                BacktestRun.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Backtest not found")
    return out(row)
