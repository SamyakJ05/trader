"""When a live strategy may run, and what a lapsed session does to it.

These exercise the real runner rather than mirroring its logic in the test,
because what is being pinned is a decision about whether real orders are
placed -- a copy of the rule could agree with itself while the runner did
something else.
"""

import uuid
from types import SimpleNamespace

import fakeredis.aioredis

from app.domain.enums import BrokerAccountStatus, StrategyStatus
from app.engines.strategy import runner


def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def strategy(**kw):
    base = dict(
        id=uuid.uuid4(), user_id=uuid.uuid4(), broker_account_id=uuid.uuid4(),
        kind="sma_crossover", status=StrategyStatus.RUNNING.value,
        symbols=["RELIANCE"], params={}, environment="live",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def account(status=BrokerAccountStatus.CONNECTED.value, environment="live"):
    return SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), broker="icici_breeze",
        environment=environment, status=status, live_enabled=True,
    )


class FakeDb:
    """Serves the runner's two queries: the RUNNING list, then the locked
    re-read of each strategy."""

    def __init__(self, strategies, account_row):
        self._strategies = list(strategies)
        self._account = account_row
        self.committed = 0

    async def execute(self, *args, **kwargs):
        rows = self._strategies

        class Result:
            def scalars(inner):
                return SimpleNamespace(all=lambda: rows, __iter__=lambda s: iter(rows))

            def scalar_one_or_none(inner):
                return rows[0] if rows else None

        return Result()

    async def get(self, model, pk):
        return self._account

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        pass


async def test_a_lapsed_session_leaves_a_live_strategy_running_and_idle(monkeypatch):
    """A broker session expiring is a routine daily event at the exchange
    flush, not a broken strategy.

    The runner used to check only that the account existed, so it went on
    evaluating: each signal reached the adapter, raised SessionExpiredError,
    and sent the strategy to ERROR -- a state meaning "needs a human". The
    user then had to restart every live strategy by hand each morning, having
    first worked out that nothing was actually wrong.
    """
    s = strategy()
    db = FakeDb([s], account(status=BrokerAccountStatus.SESSION_EXPIRED.value))

    evaluated = False

    class NeverCalled:
        kind = "sma_crossover"

        def min_history(self, params):
            nonlocal evaluated
            evaluated = True
            return 1

        def evaluate(self, ctx):
            nonlocal evaluated
            evaluated = True
            return []

    monkeypatch.setitem(runner.STRATEGY_REGISTRY, "sma_crossover", NeverCalled())

    emitted = await runner.run_once(db, redis())

    assert emitted == 0
    assert not evaluated, "a strategy with no broker session must not evaluate"
    # The important half: it is still RUNNING, so the next tick after the user
    # logs back in resumes on its own.
    assert s.status == StrategyStatus.RUNNING.value


async def test_a_connected_account_still_evaluates(monkeypatch):
    """The guard must not be so broad that it stops normal operation."""
    s = strategy()
    db = FakeDb([s], account())

    evaluated = False

    class Recording:
        kind = "sma_crossover"

        def min_history(self, params):
            return 1

        def evaluate(self, ctx):
            nonlocal evaluated
            evaluated = True
            return []

    monkeypatch.setitem(runner.STRATEGY_REGISTRY, "sma_crossover", Recording())

    async def fake_history(*a, **kw):
        return [
            # OHLCV, because a real candles row has all of them NOT NULL.
            # A close-only stub let the runner pass a context no live feed
            # could ever produce.
            SimpleNamespace(
                open=100, high=100, low=100, close=100, volume=1,
                ts=__import__("datetime").datetime.now(),
            )
        ]

    monkeypatch.setattr(runner, "candle_history", fake_history)

    async def fake_position(*a, **kw):
        return 0

    monkeypatch.setattr(runner, "_position_quantity", fake_position)

    await runner.run_once(db, redis())
    assert evaluated


async def test_a_paper_strategy_is_not_gated_on_broker_session(monkeypatch):
    """Paper does not touch the broker at all, so a lapsed session is
    irrelevant to it -- gating paper on it would stop the simulator working
    every morning for no reason."""
    s = strategy(environment="paper")
    db = FakeDb([s], account(status=BrokerAccountStatus.SESSION_EXPIRED.value,
                             environment="paper"))

    evaluated = False

    class Recording:
        kind = "sma_crossover"

        def min_history(self, params):
            return 1

        def evaluate(self, ctx):
            nonlocal evaluated
            evaluated = True
            return []

    monkeypatch.setitem(runner.STRATEGY_REGISTRY, "sma_crossover", Recording())

    async def fake_history(*a, **kw):
        return [
            # OHLCV, because a real candles row has all of them NOT NULL.
            # A close-only stub let the runner pass a context no live feed
            # could ever produce.
            SimpleNamespace(
                open=100, high=100, low=100, close=100, volume=1,
                ts=__import__("datetime").datetime.now(),
            )
        ]

    monkeypatch.setattr(runner, "candle_history", fake_history)

    async def fake_position(*a, **kw):
        return 0

    monkeypatch.setattr(runner, "_position_quantity", fake_position)

    async def fake_price(*a, **kw):
        return None

    monkeypatch.setattr(runner.market_sim, "get_price", fake_price)

    await runner.run_once(db, redis())
    assert evaluated
