"""Hard ceilings on how much capital a strategy may commit.

The existing rules cap a single order, a single symbol's position, and the
day's realized loss. None of them bounds total capital: ten orders of 50k in
ten different symbols pass MAX_ORDER_NOTIONAL and MAX_POSITION_SIZE while
committing 5 lakh.

Two ceilings, because they stop different things. EXPOSURE bounds what is
held at once, so selling frees room. TURNOVER bounds what is bought in a day
regardless of sells, which is what stops a strategy churning the same capital
and paying charges on every round trip.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fakeredis import FakeAsyncRedis

from app.domain.enums import Exchange, OrderSide, OrderType, ProductType, RiskRuleType
from app.domain.models import OrderRequest
from app.engines.risk.engine import RiskEngine


@pytest.fixture
def redis():
    return FakeAsyncRedis(decode_responses=True)


class ScalarDb:
    """Returns a fixed scalar for each aggregate query, in order."""

    def __init__(self, scalars=()):
        self._scalars = list(scalars)
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, *a, **kw):
        value = self._scalars.pop(0) if self._scalars else 0

        class Result:
            def scalar_one(inner):
                return value

        return Result()


def order(side=OrderSide.BUY, quantity=10, price="2800"):
    return OrderRequest(
        symbol="RELIANCE", exchange=Exchange.NSE, side=side,
        order_type=OrderType.LIMIT, product=ProductType.CNC,
        quantity=quantity, price=Decimal(price),
    )


def account():
    return SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), broker="icici_breeze")


async def check(db, redis, rule, params, request, environment="live"):
    engine = RiskEngine(db, redis)
    return await engine._check_rule(
        rule, params, uuid.uuid4(), account(), request, environment, Decimal("2800")
    )


# ── total exposure ───────────────────────────────────────────────────


async def test_an_order_within_the_ceiling_passes(redis):
    """50k held, 28k incoming, 100k ceiling."""
    db = ScalarDb([Decimal("50000")])
    reason = await check(
        db, redis, RiskRuleType.MAX_TOTAL_EXPOSURE, {"max_exposure": 100000}, order()
    )
    assert reason is None


async def test_an_order_over_the_ceiling_is_refused(redis):
    """The case the per-order limit misses: each order is small, the total is
    not."""
    db = ScalarDb([Decimal("90000")])
    reason = await check(
        db, redis, RiskRuleType.MAX_TOTAL_EXPOSURE, {"max_exposure": 100000}, order()
    )
    assert reason is not None
    assert "exposure" in reason
    # The message must say what is held now, or the operator cannot tell how
    # much room is left.
    assert "90000" in reason


async def test_a_sell_is_never_blocked_by_the_exposure_ceiling(redis):
    """A sell reduces exposure. Refusing one because the book is full would
    trap a strategy in exactly the position the limit exists to bound."""
    db = ScalarDb([Decimal("999999")])
    reason = await check(
        db, redis, RiskRuleType.MAX_TOTAL_EXPOSURE, {"max_exposure": 100000},
        order(side=OrderSide.SELL),
    )
    assert reason is None


async def test_exposure_refuses_when_it_cannot_value_the_order(redis):
    """An unpriced order cannot be checked against a rupee ceiling, and
    guessing would make the limit meaningless."""
    request = OrderRequest(
        symbol="RELIANCE", exchange=Exchange.NSE, side=OrderSide.BUY,
        order_type=OrderType.MARKET, product=ProductType.CNC, quantity=10,
    )
    engine = RiskEngine(ScalarDb([Decimal(0)]), redis)
    reason = await engine._check_rule(
        RiskRuleType.MAX_TOTAL_EXPOSURE, {"max_exposure": 100000},
        uuid.uuid4(), account(), request, "live", None,
    )
    assert reason is not None
    assert "reference price" in reason


# ── daily turnover ───────────────────────────────────────────────────


async def test_turnover_within_the_budget_passes(redis):
    db = ScalarDb([Decimal("100000")])
    reason = await check(
        db, redis, RiskRuleType.MAX_DAILY_TURNOVER, {"max_turnover": 200000}, order()
    )
    assert reason is None


async def test_turnover_over_the_budget_is_refused(redis):
    """What exposure alone does not stop: buy, sell, buy again. Each round
    trip frees its own exposure room while the charges accumulate."""
    db = ScalarDb([Decimal("190000")])
    reason = await check(
        db, redis, RiskRuleType.MAX_DAILY_TURNOVER, {"max_turnover": 200000}, order()
    )
    assert reason is not None
    assert "turnover" in reason
    assert "midnight IST" in reason


async def test_a_sell_does_not_consume_the_turnover_budget(redis):
    db = ScalarDb([Decimal("999999")])
    reason = await check(
        db, redis, RiskRuleType.MAX_DAILY_TURNOVER, {"max_turnover": 200000},
        order(side=OrderSide.SELL),
    )
    assert reason is None


# ── the defaults every account now gets ──────────────────────────────


def test_both_ceilings_are_in_the_defaults():
    """The engine loops over ENABLED rules, so a user with none passes every
    check. Only the demo seeder created any, and a production deploy never
    runs it -- so a real account had no limit of any kind."""
    from app.services.risk_defaults import DEFAULT_RULES

    types = {rule for rule, _ in DEFAULT_RULES}
    assert RiskRuleType.MAX_TOTAL_EXPOSURE in types
    assert RiskRuleType.MAX_DAILY_TURNOVER in types
    assert RiskRuleType.MAX_DAILY_LOSS in types


def test_the_defaults_are_conservative():
    """A floor a person raises knowingly, not a ceiling tuned for a strategy."""
    from app.services.risk_defaults import DEFAULT_RULES

    params = dict(DEFAULT_RULES)
    assert params[RiskRuleType.MAX_TOTAL_EXPOSURE]["max_exposure"] <= 100000
    assert params[RiskRuleType.MAX_ORDER_NOTIONAL]["max_notional"] <= 25000


async def test_provisioning_covers_both_environments():
    """An account can be switched between them, and a rule set that exists in
    one but not the other is a limit that silently disappears."""
    from app.services import risk_defaults

    class CollectingDb:
        def __init__(self):
            self.added = []

        def add(self, obj):
            self.added.append(obj)

    db = CollectingDb()
    created = await risk_defaults.provision(db, uuid.uuid4())
    environments = {r.environment for r in db.added}
    assert environments == {"paper", "live"}
    assert created == len(db.added)
