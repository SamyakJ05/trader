"""Portfolio risk ceilings under concurrent orders.

The capital rules are read-then-act: each sums what is committed and compares
the total against a ceiling. Evaluated concurrently without a lock, every
order reads the same "before" figure and every one concludes there is room --
so orders that are individually under the ceiling pass together and land over
it. The unit tests cannot see this: they drive _check_rule against a fake db
that returns canned scalars, which has no transactions to interleave.

These use a real Postgres because the defect only exists in the database's
concurrency semantics, and a lock that is not exercised by two live
transactions has not been tested at all.
"""

import asyncio
import os
import uuid
from decimal import Decimal as D

import fakeredis.aioredis
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import BrokerAccount, Order, Position, RiskRule, User
from app.domain.enums import (
    Environment,
    Exchange,
    OrderSide,
    OrderType,
    ProductType,
    RiskDecision,
    RiskRuleType,
)
from app.domain.models import OrderRequest
from app.engines.risk.engine import RiskEngine

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="requires disposable Postgres"
)


@pytest.fixture
async def sessions():
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def redis():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield redis
    await redis.aclose()


@pytest.fixture
async def account(sessions):
    """A live-environment account whose user holds 150k of a 200k ceiling."""
    async with sessions() as db:
        user = User(email=f"{uuid.uuid4()}@test.local", password_hash="test")
        db.add(user)
        await db.flush()
        account = BrokerAccount(
            user_id=user.id,
            broker="paper",
            environment=Environment.LIVE.value,
            label="test",
        )
        db.add(account)
        await db.flush()
        db.add(
            RiskRule(
                user_id=user.id,
                environment=Environment.LIVE.value,
                rule_type=RiskRuleType.MAX_TOTAL_EXPOSURE.value,
                params={"max_exposure": 200000},
                enabled=True,
            )
        )
        # 150k already held: one more 40k buy fits, five do not.
        db.add(
            Position(
                user_id=user.id,
                broker_account_id=account.id,
                environment=Environment.LIVE.value,
                symbol="HELD",
                exchange="NSE",
                product="CNC",
                quantity=1500,
                average_price=D(100),
            )
        )
        await db.commit()
        return account


def buy(symbol, quantity=40, price="1000"):
    return OrderRequest(
        symbol=symbol,
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        product=ProductType.CNC,
        quantity=quantity,
        price=D(price),
    )


async def test_concurrent_buys_cannot_breach_the_exposure_ceiling(sessions, redis, account):
    """Five concurrent 40k buys against 50k of remaining room: one may pass.

    Each order is individually legal -- 150k held + 40k = 190k, under the
    200k ceiling. Only their sum breaches it, and only a lock that makes the
    evaluations take turns can see that.

    Every ALLOW writes its position in the same transaction that evaluated it,
    which is what a real order does between the risk check and the commit; a
    check that allowed while writing nothing would serialize trivially and
    prove nothing.
    """

    async def attempt(n):
        async with sessions() as db:
            engine = RiskEngine(db, redis)
            result = await engine.evaluate(
                user_id=account.user_id,
                account=account,
                request=buy(f"SYM{n}"),
                environment=Environment.LIVE.value,
                last_price=D(1000),
                client_order_id=f"c{n}",
            )
            if result.decision == RiskDecision.ALLOW.value:
                db.add(
                    Position(
                        user_id=account.user_id,
                        broker_account_id=account.id,
                        environment=Environment.LIVE.value,
                        symbol=f"SYM{n}",
                        exchange="NSE",
                        product="CNC",
                        quantity=40,
                        average_price=D(1000),
                    )
                )
            await db.commit()
            return result.decision

    decisions = await asyncio.gather(*(attempt(n) for n in range(5)))

    allowed = [d for d in decisions if d == RiskDecision.ALLOW.value]
    assert len(allowed) == 1, (
        f"expected exactly one buy to fit in the 50k of remaining room, got {len(allowed)}: "
        f"{decisions}"
    )

    async with sessions() as db:
        committed = await db.scalar(
            select(func.coalesce(func.sum(Position.quantity * Position.average_price), 0)).where(
                Position.user_id == account.user_id,
                Position.environment == Environment.LIVE.value,
            )
        )
    assert D(str(committed)) <= D(200000), f"exposure ceiling breached: {committed}"


async def test_concurrent_auto_trades_cannot_breach_the_daily_cap(sessions, redis, account):
    """The auto-trade counter is a ceiling like any other, and races like one.

    Counting rows and comparing against a cap is the same read-then-act shape
    as the capital rules, so it is held by the same lock; without it, N
    concurrent auto orders all count the same total and all pass.
    """
    async with sessions() as db:
        db.add(
            RiskRule(
                user_id=account.user_id,
                environment=Environment.LIVE.value,
                rule_type=RiskRuleType.MAX_AUTO_TRADES_PER_DAY.value,
                params={"max_auto_trades": 2},
                enabled=True,
            )
        )
        # Raise the capital ceiling so this test measures the count rule only.
        rule = (
            await db.execute(
                select(RiskRule).where(
                    RiskRule.user_id == account.user_id,
                    RiskRule.rule_type == RiskRuleType.MAX_TOTAL_EXPOSURE.value,
                )
            )
        ).scalar_one()
        rule.params = {"max_exposure": 10_000_000}
        await db.commit()

    async def attempt(n):
        async with sessions() as db:
            engine = RiskEngine(db, redis)
            result = await engine.evaluate(
                user_id=account.user_id,
                account=account,
                request=buy(f"AUTO{n}", quantity=1),
                environment=Environment.LIVE.value,
                last_price=D(1000),
                client_order_id=f"a{n}",
                auto_executed=True,
            )
            if result.decision == RiskDecision.ALLOW.value:
                db.add(
                    Order(
                        user_id=account.user_id,
                        broker_account_id=account.id,
                        environment=Environment.LIVE.value,
                        client_order_id=f"a{n}",
                        symbol=f"AUTO{n}",
                        exchange="NSE",
                        side="BUY",
                        order_type="LIMIT",
                        product="CNC",
                        quantity=1,
                        price=D(1000),
                        status="ACCEPTED",
                        auto_executed=True,
                    )
                )
            await db.commit()
            return result.decision

    decisions = await asyncio.gather(*(attempt(n) for n in range(5)))
    allowed = [d for d in decisions if d == RiskDecision.ALLOW.value]
    assert len(allowed) == 2, f"cap of 2 auto trades admitted {len(allowed)}: {decisions}"
