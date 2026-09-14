"""Real transaction tests. Set TEST_DATABASE_URL to a migrated disposable Postgres DB."""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import fakeredis.aioredis
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import (
    BrokerAccount,
    CashLedger,
    Fill,
    FundsSnapshot,
    Order,
    PaperHolding,
    PendingSettlement,
    Position,
    User,
)
from app.engines.paper import engine as paper
from app.engines.paper import ledger, settlement

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="requires disposable Postgres"
)


@pytest.fixture
async def sessions():
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def account(sessions):
    async with sessions() as db:
        user = User(email=f"{uuid.uuid4()}@test.local", password_hash="test")
        db.add(user)
        await db.flush()
        account = BrokerAccount(user_id=user.id, broker="paper", environment="paper", label="test")
        db.add(account)
        await db.commit()
        return account


def order(account, side="BUY", product="MIS", quantity=10):
    return Order(
        user_id=account.user_id,
        broker_account_id=account.id,
        environment="paper",
        client_order_id=uuid.uuid4().hex,
        symbol="TEST",
        exchange="NSE",
        side=side,
        order_type="MARKET",
        product=product,
        quantity=quantity,
        status="ACCEPTED",
    )


@pytest.fixture
async def redis(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(paper, "PARTIAL_FILL_P", 0)
    await redis.set("sim:price:TEST", "100")
    yield redis
    await redis.aclose()


async def fill_order(db, redis, account, **kwargs):
    row = order(account, **kwargs)
    db.add(row)
    await db.flush()
    await paper.try_fill_order(db, redis, row)
    await db.commit()
    return row


async def test_ledger_sum_and_reset_history(sessions, account):
    async with sessions() as db:
        db.add(FundsSnapshot(broker_account_id=account.id, available_cash=D("125.00")))
        await db.commit()
        await ledger.append(db, account.id, "BUY", D("-20"))
        await ledger.append(db, account.id, "CHARGES", D("-1.25"))
        await db.commit()
        assert await ledger.get_cash(db, account.id) == D("103.75")
        await ledger.reset(db, account.id)
        await db.commit()
        entries = (
            (
                await db.execute(
                    select(CashLedger)
                    .where(CashLedger.broker_account_id == account.id)
                    .order_by(CashLedger.id)
                )
            )
            .scalars()
            .all()
        )
        assert [e.entry_type for e in entries] == ["OPENING", "BUY", "CHARGES", "RESET", "OPENING"]
        assert sum(e.amount for e in entries) == entries[-1].balance == D("1000000")


@pytest.mark.parametrize(
    "operation",
    [
        "UPDATE cash_ledger SET amount=0 WHERE broker_account_id=:id",
        "DELETE FROM cash_ledger WHERE broker_account_id=:id",
    ],
)
async def test_database_enforces_append_only(sessions, account, operation):
    async with sessions() as db:
        await ledger.append(db, account.id, "CHARGES", D(-1))
        await db.commit()
        with pytest.raises(Exception, match="append-only"):
            await db.execute(text(operation), {"id": account.id})
        await db.rollback()


async def test_concurrent_ledger_writers(sessions, account):
    async def debit():
        async with sessions() as db:
            await ledger.append(db, account.id, "CHARGES", D(-10))
            await db.commit()

    await asyncio.gather(debit(), debit(), debit())
    async with sessions() as db:
        assert await ledger.get_cash(db, account.id) == D(999970)
        count = await db.scalar(
            select(func.count())
            .select_from(CashLedger)
            .where(CashLedger.broker_account_id == account.id, CashLedger.entry_type == "OPENING")
        )
        assert count == 1


async def test_delivery_settles_once_and_sell_debits_holding(sessions, account, redis):
    async with sessions() as db:
        buy = await fill_order(db, redis, account, product="CNC")
        assert buy.status == "FILLED"
        pending = (
            await db.execute(
                select(PendingSettlement).where(PendingSettlement.broker_account_id == account.id)
            )
        ).scalar_one()
        sell = await fill_order(db, redis, account, side="SELL", product="CNC")
        assert sell.status == "REJECTED"
        assert "settled" in sell.status_message
        future = datetime.combine(pending.settles_on, datetime.min.time(), tzinfo=timezone.utc)
        assert await settlement.settle_account(db, account.id, future) == 1
        assert await settlement.settle_account(db, account.id, future) == 0
        await db.commit()
        holding = await settlement.get_holding(db, account.id, "TEST", "NSE")
        assert holding.quantity == 10
        position = (
            await db.execute(select(Position).where(Position.broker_account_id == account.id))
        ).scalar_one()
        assert position.quantity == 0
        sell = await fill_order(db, redis, account, side="SELL", product="CNC", quantity=4)
        assert sell.status == "FILLED"
        await db.refresh(holding)
        assert holding.quantity == 6
        entries = (
            (await db.execute(select(CashLedger).where(CashLedger.broker_account_id == account.id)))
            .scalars()
            .all()
        )
        assert sum(e.amount for e in entries) == await ledger.get_cash(db, account.id)


async def test_reset_cancels_settlement(sessions, account, redis):
    async with sessions() as db:
        await fill_order(db, redis, account, product="CNC")
        await paper.reset_account(db, account)
        await db.commit()
        assert (
            await settlement.settle_account(
                db, account.id, datetime.now(timezone.utc) + timedelta(days=30)
            )
            == 0
        )
        assert await settlement.get_holding(db, account.id, "TEST", "NSE") is None
        assert await ledger.get_cash(db, account.id) == D(1000000)


async def test_insufficient_cash_has_no_fill(sessions, account, redis):
    async with sessions() as db:
        await ledger.append(db, account.id, "RESET", D(-1000000))
        await db.commit()
        row = await fill_order(db, redis, account)
        assert row.status == "REJECTED"
        assert (
            await db.scalar(select(func.count()).select_from(Fill).where(Fill.order_id == row.id))
            == 0
        )
        assert await ledger.get_cash(db, account.id) == 0


async def test_two_workers_do_not_fill_same_order_twice(sessions, account, redis):
    async with sessions() as db:
        row = order(account)
        db.add(row)
        await db.commit()
        order_id = row.id

    async def attempt():
        async with sessions() as db:
            row = await db.get(Order, order_id)
            result = await paper.try_fill_order(db, redis, row)
            await db.commit()
            return result

    assert sorted(await asyncio.gather(attempt(), attempt())) == [False, True]
    async with sessions() as db:
        assert (
            await db.scalar(select(func.count()).select_from(Fill).where(Fill.order_id == order_id))
            == 1
        )


async def test_competing_sells_cannot_oversell(sessions, account, redis):
    async with sessions() as db:
        db.add(
            PaperHolding(
                broker_account_id=account.id,
                symbol="TEST",
                exchange="NSE",
                quantity=10,
                average_price=D(100),
            )
        )
        await db.commit()

    async def sell():
        async with sessions() as db:
            return (
                await fill_order(db, redis, account, side="SELL", product="CNC", quantity=10)
            ).status

    assert sorted(await asyncio.gather(sell(), sell())) == ["FILLED", "REJECTED"]


async def test_tick_ohlc_out_of_order_and_source_isolation(sessions):
    from app.db.models import Candle
    from app.domain.models import Tick
    from app.domain.enums import Exchange
    from app.engines.market.candles import record_tick, history, store_candle

    symbol = uuid.uuid4().hex
    start = datetime(2026, 9, 1, 4, tzinfo=timezone.utc)
    async with sessions() as db:
        for seconds, price in [(20, 120), (10, 100), (40, 110), (30, 130)]:
            await record_tick(
                db,
                Tick(
                    symbol=symbol,
                    exchange=Exchange.NSE,
                    ts=start + timedelta(seconds=seconds),
                    last_price=D(price),
                ),
            )
        await store_candle(
            db,
            symbol=symbol,
            exchange="NSE",
            interval="1m",
            source="yfinance_unadjusted",
            ts=start,
            open=D(999),
            high=D(999),
            low=D(999),
            close=D(999),
            volume=100,
        )
        await db.commit()
        bars = await history(db, symbol, "NSE", "1m", "simulator", 10, start + timedelta(minutes=1))
        assert len(bars) == 1
        bar = bars[0]
        assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (100, 130, 100, 110, None)
        assert await history(db, symbol, "NSE", "1m", "simulator", 10, start) == []
        for minute in range(1, 6):
            await record_tick(
                db,
                Tick(
                    symbol=symbol,
                    exchange=Exchange.NSE,
                    ts=start + timedelta(minutes=minute),
                    last_price=D(100 + minute),
                ),
            )
        await db.commit()
        rolled = (
            await db.execute(
                select(Candle).where(
                    Candle.symbol == symbol, Candle.source == "simulator", Candle.interval == "5m"
                )
            )
        ).scalar_one()
        assert (rolled.open, rolled.high, rolled.low, rolled.close) == (100, 130, 100, 104)


async def test_saved_backtest_isolation(sessions, account):
    from app.api.routes.backtests import get_backtest, list_backtests
    from app.db.models import BacktestRun
    from fastapi import HTTPException
    from types import SimpleNamespace

    async with sessions() as db:
        run = BacktestRun(
            user_id=account.user_id, kind="sma_crossover", config={}, results={"total_return": "0"}
        )
        db.add(run)
        await db.commit()
        owner = SimpleNamespace(id=account.user_id)
        other = SimpleNamespace(id=uuid.uuid4())
        assert (await get_backtest(run.id, owner, db))["id"] == str(run.id)
        assert await list_backtests(other, db) == []
        with pytest.raises(HTTPException) as exc:
            await get_backtest(run.id, other, db)
        assert exc.value.status_code == 404


async def test_portfolio_cash_ignores_stale_snapshot(sessions, account):
    from app.api.routes.portfolio import funds, cash_entries
    from types import SimpleNamespace
    from fastapi import HTTPException

    async with sessions() as db:
        db.add(FundsSnapshot(broker_account_id=account.id, available_cash=D(100)))
        await db.commit()
        await ledger.append(db, account.id, "CHARGES", D(-5))
        await db.commit()
        owner = SimpleNamespace(id=account.user_id)
        assert (await funds(owner, db, account.id))["available_cash"] == "95.00"
        with pytest.raises(HTTPException) as exc:
            await cash_entries(SimpleNamespace(id=uuid.uuid4()), db, account.id)
        assert exc.value.status_code == 404


async def test_holdings_count_for_position_risk(sessions, account, redis):
    from app.engines.risk.engine import RiskEngine
    from app.domain.enums import RiskRuleType, Exchange, OrderSide, ProductType, OrderType
    from app.domain.models import OrderRequest

    async with sessions() as db:
        db.add(
            PaperHolding(
                broker_account_id=account.id,
                symbol="TEST",
                exchange="NSE",
                quantity=10,
                average_price=D(100),
            )
        )
        await db.commit()
        request = OrderRequest(
            symbol="TEST",
            exchange=Exchange.NSE,
            side=OrderSide.BUY,
            product=ProductType.CNC,
            order_type=OrderType.MARKET,
            quantity=1,
        )
        risk = RiskEngine(db, redis)
        assert (
            await risk._check_rule(
                RiskRuleType.MAX_POSITION_SIZE,
                {"max_quantity": 10},
                account.user_id,
                account,
                request,
                "paper",
                D(100),
            )
            is not None
        )
        request.symbol = "NEW"
        assert (
            await risk._check_rule(
                RiskRuleType.MAX_OPEN_POSITIONS,
                {"max_positions": 1},
                account.user_id,
                account,
                request,
                "paper",
                D(100),
            )
            is not None
        )
        request.symbol, request.side = "TEST", OrderSide.SELL
        assert (
            await risk._check_rule(
                RiskRuleType.MAX_OPEN_POSITIONS,
                {"max_positions": 1},
                account.user_id,
                account,
                request,
                "paper",
                D(100),
            )
            is None
        )


async def test_backtest_api_persists_next_open_results(sessions, account):
    from app.api.routes.backtests import BacktestBody, create_backtest
    from app.engines.market.candles import store_candle
    from types import SimpleNamespace

    symbol = uuid.uuid4().hex.upper()
    start = datetime(2025, 2, 1, tzinfo=timezone.utc)
    async with sessions() as db:
        for i, price in enumerate([3, 2, 1, 3, 2, 1, 3, 2]):
            await store_candle(
                db,
                symbol=symbol,
                exchange="NSE",
                interval="1m",
                source="simulator",
                ts=start + timedelta(minutes=i),
                open=D(price),
                close=D(price),
                high=D(price),
                low=D(price),
                volume=None,
            )
        await db.commit()
        result = await create_backtest(
            BacktestBody(
                symbol=symbol,
                interval="1m",
                source="simulator",
                start=start,
                end=start + timedelta(days=1),
                fast=1,
                slow=2,
            ),
            SimpleNamespace(id=account.user_id),
            db,
        )
        assert result["results"]["candle_count"] == 8
        assert len(result["results"]["data_sha256"]) == 64
        assert result["results"]["fills"][0]["ts"] == (start + timedelta(minutes=4)).isoformat()
        assert D(result["results"]["fills"][0]["price"]) == 2


async def test_strategy_uses_completed_candles_once(sessions, account, redis, monkeypatch):
    from app.db.models import Strategy
    from app.engines.market.candles import store_candle
    from app.engines.strategy import runner
    from unittest.mock import AsyncMock

    symbol = uuid.uuid4().hex.upper()
    start = datetime(2025, 2, 1, tzinfo=timezone.utc)
    acted = AsyncMock()
    monkeypatch.setattr(runner, "_act_on_signal", acted)
    async with sessions() as db:
        strategy = Strategy(
            user_id=account.user_id,
            broker_account_id=account.id,
            name="once",
            kind="sma_crossover",
            symbols=[symbol],
            status="RUNNING",
            params={"fast": 1, "slow": 2},
        )
        db.add(strategy)
        for i, price in enumerate([3, 2, 1, 3]):
            await store_candle(
                db,
                symbol=symbol,
                exchange="NSE",
                interval="1m",
                source="simulator",
                ts=start + timedelta(minutes=i),
                open=D(price),
                close=D(price),
                high=D(price),
                low=D(price),
                volume=None,
            )
        await db.commit()
        assert await runner.run_once(db, redis) == 1
        assert await runner.run_once(db, redis) == 0
        assert acted.await_count == 1
        strategy.status = "STOPPED"
        await db.commit()


async def test_missing_calendar_rejects_without_cash_movement(
    sessions, account, redis, monkeypatch
):
    from app.domain.calendar import CalendarUnavailable

    def unavailable(day):
        raise CalendarUnavailable("No holiday list")

    monkeypatch.setattr(paper, "settlement_date", unavailable)
    async with sessions() as db:
        row = await fill_order(db, redis, account, product="CNC")
        assert row.status == "REJECTED"
        assert await ledger.get_cash(db, account.id) == D(1000000)
        assert (
            await db.scalar(select(func.count()).select_from(Fill).where(Fill.order_id == row.id))
            == 0
        )


async def test_concurrent_order_inserts_do_not_deadlock_cash_lock(sessions, account, redis):
    ready = asyncio.Barrier(2)

    async def place():
        async with sessions() as db:
            await db.execute(text("SET LOCAL lock_timeout = '3s'"))
            row = order(account)
            db.add(row)
            await db.flush()  # Both transactions now hold FK KEY SHARE locks.
            await ready.wait()
            result = await paper.try_fill_order(db, redis, row)
            await db.commit()
            return result

    assert await asyncio.gather(place(), place()) == [True, True]
