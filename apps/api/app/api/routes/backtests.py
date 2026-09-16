import hashlib
import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal

from arq.connections import RedisSettings, create_pool
from arq.jobs import Job, JobStatus
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.deps import DbSession, VerifiedUser
from app.db.models import BacktestRun, BrokerAccount, Candle, Strategy
from app.domain.calendar import CalendarUnavailable
from app.domain.enums import Broker
from app.engines.backtest import run_backtest
from app.engines.market.candles import bucket_start
from app.engines.strategy.base import AsyncStrategyBase
from app.engines.strategy.runner import STRATEGY_REGISTRY
from app.services import instruments as instrument_service

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
    return await _run(
        db,
        user,
        kind=body.kind,
        impl=STRATEGY_REGISTRY[body.kind],
        symbol=body.symbol,
        exchange=body.exchange,
        product=body.product,
        interval=body.interval,
        source=body.source,
        start=body.start,
        end=body.end,
        initial_cash=body.initial_cash,
        params=dict(fast=body.fast, slow=body.slow, quantity=body.quantity),
        broker=body.broker,
        config=body.model_dump(mode="json"),
    )


async def _run(
    db,
    user,
    *,
    kind: str,
    impl,
    symbol: str,
    exchange: str,
    product: str,
    interval: str,
    source: str,
    start: datetime,
    end: datetime,
    initial_cash: Decimal,
    params: dict,
    broker: str,
    config: dict,
):
    """Fetch candles, replay, persist. Shared so a strategy-driven run and a
    hand-configured one cannot drift apart in how they price or score."""
    cutoff = min(end, bucket_start(datetime.now(timezone.utc), interval))
    candles = (
        (
            await db.execute(
                select(Candle)
                .where(
                    Candle.symbol == symbol,
                    Candle.exchange == exchange,
                    Candle.interval == interval,
                    Candle.source == source,
                    Candle.ts >= start,
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
    warmup = impl.min_history(params)
    if len(candles) < warmup + 2:
        raise HTTPException(
            422,
            f"Not enough completed candles: {len(candles)} found, "
            f"{warmup + 2} needed for warmup and a next-open fill. "
            f"Import history for {symbol} first.",
        )
    try:
        result = await run_in_threadpool(
            run_backtest,
            impl,
            candles,
            symbol=symbol,
            exchange=exchange,
            product=product,
            initial_cash=initial_cash,
            params=params,
            # Metrics annualise by sqrt(periods per year), so the bar size has
            # to reach them: scoring minute bars as daily understates Sharpe by
            # roughly twenty times.
            interval=interval,
            broker=Broker(broker),
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
        source=source,
        first_candle=candles[0].ts.isoformat(),
        last_candle=candles[-1].ts.isoformat(),
    )
    row = BacktestRun(user_id=user.id, kind=kind, config=config, results=result)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return out(row)


class StrategyBacktestBody(BaseModel):
    """Backtest a strategy that already exists, rather than a fresh config.

    The only way to run a backtest was to retype an SMA configuration by
    hand, which meant the strategy you actually intended to trade was never
    the thing tested. This reads the symbol, params and broker off the saved
    strategy so the run describes it.
    """

    strategy_id: uuid.UUID
    start: datetime
    end: datetime
    interval: Literal["1m", "5m", "1d"] = "1d"
    source: Literal["simulator", "yfinance_unadjusted"] = "yfinance_unadjusted"
    initial_cash: Decimal = Field(default=Decimal("1000000"), gt=0, le=Decimal("1000000000"))
    # Which of the strategy's symbols to test. A strategy may name several and
    # a backtest runs one instrument at a time.
    symbol: str | None = None

    @model_validator(mode="after")
    def validate_range(self):
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Start and end require timezone")
        if self.start >= self.end:
            raise ValueError("Start must precede end")
        return self


@router.post("/strategy", status_code=201)
async def backtest_strategy(
    body: StrategyBacktestBody, user: VerifiedUser, db: DbSession
):
    strategy = (
        await db.execute(
            select(Strategy).where(
                Strategy.id == body.strategy_id, Strategy.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if strategy is None:
        raise HTTPException(404, "Strategy not found")

    impl = STRATEGY_REGISTRY.get(strategy.kind)
    if impl is None:
        raise HTTPException(422, f"Unknown strategy kind {strategy.kind!r}")
    if isinstance(impl, AsyncStrategyBase):
        # An LLM strategy's decisions depend on context that no longer exists
        # and were never recorded, so replaying it would be inventing them.
        raise HTTPException(
            422,
            f"{strategy.kind} decides with an LLM and cannot be replayed: its "
            "historical decisions were not recorded. Backtest a rule-based "
            "strategy, or run this one on paper to gather a track record.",
        )

    symbol = body.symbol or (strategy.symbols[0] if strategy.symbols else None)
    if not symbol:
        raise HTTPException(422, "Strategy names no symbols")
    if body.symbol and body.symbol not in strategy.symbols:
        raise HTTPException(422, f"{body.symbol} is not one of this strategy's symbols")

    params = dict(strategy.params or {})
    exchange = str(params.get("exchange", "NSE")).upper()
    product = str(params.get("product", "MIS")).upper()
    if product not in ("MIS", "CNC"):
        raise HTTPException(
            422, f"Backtests cover MIS and CNC equity only; this strategy uses {product}"
        )

    account = None
    if strategy.broker_account_id is not None:
        account = await db.get(BrokerAccount, strategy.broker_account_id)
    broker = account.broker if account else Broker.PAPER.value

    # Breeze names RELIND where imported history is stored as RELIANCE, so
    # the strategy's own symbol matches no candle. Resolved through the ISIN,
    # which is the same at every venue.
    history_symbol = await instrument_service.history_symbol(
        db, broker=broker, symbol=symbol, exchange=exchange
    )

    return await _run(
        db,
        user,
        kind=strategy.kind,
        impl=impl,
        symbol=history_symbol,
        exchange=exchange,
        product=product,
        interval=body.interval,
        source=body.source,
        start=body.start,
        end=body.end,
        initial_cash=body.initial_cash,
        params=params,
        broker=broker,
        config=dict(
            body.model_dump(mode="json"),
            strategy_name=strategy.name,
            requested_symbol=symbol,
            history_symbol=history_symbol,
            broker=broker,
        ),
    )


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


# ── history import ───────────────────────────────────────────────────


class ImportHistoryBody(BaseModel):
    """Import market history so a backtest has bars to replay.

    Symbols are NSE tickers, which is what the data source keys on. A Breeze
    strategy names ICICI's own codes (RELIND), and the backtest resolves those
    to the ticker through the instrument master's ISIN -- so import the
    ticker, not the broker code.
    """

    # Capped because each symbol is a separate slow download; a request naming
    # fifty would run for many minutes and the operator would have no idea how
    # far it had got.
    symbols: list[str] = Field(min_length=1, max_length=10)
    interval: Literal["1m", "5m", "1d"] = "1d"
    start: date
    end: date

    @model_validator(mode="after")
    def validate_range(self):
        if self.start >= self.end:
            raise ValueError("Start must precede end")
        self.symbols = [s.strip().upper() for s in self.symbols if s.strip()]
        if not self.symbols:
            raise ValueError("Name at least one symbol")
        return self


@router.post("/history", status_code=202)
async def import_history(body: ImportHistoryBody, user: VerifiedUser):
    """Queue a history import. Returns a job id to poll.

    202 rather than 200: the work has been accepted, not done. The download
    is slow enough that holding the request open would time out behind the
    proxy, so it runs in the worker and the client polls.
    """
    try:
        pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    except Exception as exc:  # pragma: no cover - redis down is an outage
        raise HTTPException(503, f"Job queue unavailable: {exc}") from exc
    try:
        job = await pool.enqueue_job(
            "import_history_job",
            user_id=str(user.id),
            symbols=body.symbols,
            interval=body.interval,
            start=body.start.isoformat(),
            end=body.end.isoformat(),
        )
    finally:
        await pool.aclose()
    if job is None:  # pragma: no cover - arq returns None only on a dedupe clash
        raise HTTPException(409, "That import is already queued")
    return {"job_id": job.job_id, "symbols": body.symbols, "status": "queued"}


@router.get("/history/{job_id}")
async def import_history_status(job_id: str, user: VerifiedUser):
    """Where a queued import has got to.

    A finished job reports per-symbol results, including the ones that
    failed: a delisted ticker or a range Yahoo has no data for must not look
    like success, and the operator needs to know which symbols actually
    landed.
    """
    try:
        pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    except Exception as exc:  # pragma: no cover
        raise HTTPException(503, f"Job queue unavailable: {exc}") from exc
    try:
        job = Job(job_id, pool)
        status = await job.status()
        if status == JobStatus.not_found:
            raise HTTPException(404, "Job not found — it may have expired")
        if status is not JobStatus.complete:
            return {"job_id": job_id, "status": status.value}
        try:
            result = await job.result(timeout=0)
        except Exception as exc:
            # The job raised. Reported as a failed status rather than a 500:
            # the request itself succeeded, and the operator needs the reason.
            return {"job_id": job_id, "status": "failed", "error": str(exc)[:300]}
    finally:
        await pool.aclose()
    return {"job_id": job_id, "status": "complete", **result}
