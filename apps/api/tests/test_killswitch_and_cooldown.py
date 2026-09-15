import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fakeredis import FakeAsyncRedis

from app.domain.enums import Exchange, OrderSide, OrderType, ProductType, RiskRuleType
from app.domain.models import OrderRequest
from app.engines.risk.engine import RiskEngine
from app.services import daily_pnl, killswitch


class StubDb:
    """audit.emit only needs .add(); kill switch tests don't touch SQL."""

    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)


@pytest.fixture
def redis():
    return FakeAsyncRedis(decode_responses=True)


async def test_global_killswitch_engage_release(redis):
    db = StubDb()
    user_id = uuid.uuid4()
    assert not await killswitch.is_global_engaged(redis)

    await killswitch.set_global(redis, db, engaged=True, user_id=user_id, reason="test halt")
    assert await killswitch.is_global_engaged(redis)
    status = await killswitch.status(redis)
    assert status["global_engaged"] and status["global_reason"] == "test halt"
    assert len(db.added) == 1  # audited

    await killswitch.set_global(redis, db, engaged=False, user_id=user_id, reason="release")
    assert not await killswitch.is_global_engaged(redis)
    assert len(db.added) == 2  # release audited too


async def test_strategy_killswitch_is_scoped(redis):
    db = StubDb()
    user_id, strategy_a, strategy_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await killswitch.set_strategy(
        redis, db, strategy_id=strategy_a, engaged=True, user_id=user_id, reason="runaway"
    )
    assert await killswitch.is_strategy_engaged(redis, strategy_a)
    assert not await killswitch.is_strategy_engaged(redis, strategy_b)
    assert not await killswitch.is_global_engaged(redis)
    status = await killswitch.status(redis)
    assert str(strategy_a) in status["killed_strategies"]


def order_request():
    return OrderRequest(
        symbol="RELIANCE",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product=ProductType.MIS,
        quantity=10,
    )


async def test_duplicate_cooldown_blocks_within_window(redis):
    engine = RiskEngine(db=None, redis=redis)
    account = SimpleNamespace(id=uuid.uuid4())
    request = order_request()
    rule = SimpleNamespace(
        rule_type=RiskRuleType.DUPLICATE_ORDER_COOLDOWN.value, params={"seconds": 30}
    )

    # Not armed yet: rule passes.
    reason = await engine._check_rule(
        RiskRuleType.DUPLICATE_ORDER_COOLDOWN, {}, uuid.uuid4(), account, request, "paper", None
    )
    assert reason is None

    await engine._arm_cooldown(account, request, [rule])
    reason = await engine._check_rule(
        RiskRuleType.DUPLICATE_ORDER_COOLDOWN, {}, uuid.uuid4(), account, request, "paper", None
    )
    assert reason is not None and "cooldown" in reason.lower()

    # Different side is a different key — not a duplicate.
    sell = request.model_copy(update={"side": OrderSide.SELL})
    reason = await engine._check_rule(
        RiskRuleType.DUPLICATE_ORDER_COOLDOWN, {}, uuid.uuid4(), account, sell, "paper", None
    )
    assert reason is None


async def test_notional_rule_refuses_unpriceable_order(redis):
    """Regression: market order with no reference price must be blocked,
    not crash the risk engine (and never pass unvalued)."""
    engine = RiskEngine(db=None, redis=redis)
    account = SimpleNamespace(id=uuid.uuid4())
    request = order_request()  # MARKET, price=None

    reason = await engine._check_rule(
        RiskRuleType.MAX_ORDER_NOTIONAL, {"max_notional": 100000},
        uuid.uuid4(), account, request, "paper", None,
    )
    assert reason is not None and "reference price" in reason

    # With a price, the limit applies normally.
    reason = await engine._check_rule(
        RiskRuleType.MAX_ORDER_NOTIONAL, {"max_notional": 1000},
        uuid.uuid4(), account, request, "paper", Decimal("2500"),
    )
    assert reason is not None and "exceeds limit" in reason

    reason = await engine._check_rule(
        RiskRuleType.MAX_ORDER_NOTIONAL, {"max_notional": 100000},
        uuid.uuid4(), account, request, "paper", Decimal("2500"),
    )
    assert reason is None


async def test_daily_pnl_counter_accumulates(redis):
    user_id = uuid.uuid4()
    assert await daily_pnl.get_realized(redis, user_id, "paper") == Decimal("0")
    await daily_pnl.add_realized(redis, user_id, "paper", Decimal("150.50"))
    await daily_pnl.add_realized(redis, user_id, "paper", Decimal("-400.25"))
    total = await daily_pnl.get_realized(redis, user_id, "paper")
    assert total == Decimal("-249.75")
    # Environments are isolated.
    assert await daily_pnl.get_realized(redis, user_id, "live") == Decimal("0")
