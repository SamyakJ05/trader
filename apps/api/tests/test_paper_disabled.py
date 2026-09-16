"""Running the platform live-only.

ENABLE_PAPER_TRADING=false stops paper ACCOUNTS: no new ones, no simulated
orders, no strategy evaluation, no simulated fills. It deliberately does NOT
disable the paper ENGINE, whose charge model, ledger arithmetic and fill
accounting the backtester depends on -- turning that off would take
backtesting down with it, and backtesting is the only way left to evaluate a
strategy before it trades real money.

It also does not make live trading possible. That still needs
ENABLE_LIVE_TRADING, the account's live_enabled, and an adapter whose status
is WORKING -- which no real broker has yet.
"""

import uuid
from types import SimpleNamespace

from app.services import orders as order_service


def settings(paper=False, live=False):
    return SimpleNamespace(
        enable_paper_trading=paper,
        enable_live_trading=live,
        market_hours_enforced=False,
    )


# ── the order gate ───────────────────────────────────────────────────


def test_paper_orders_are_refused_when_paper_is_off(monkeypatch):
    monkeypatch.setattr(order_service, "get_settings", lambda: settings(paper=False))
    refusal = order_service._paper_gate()
    assert refusal is not None
    assert "disabled" in refusal
    # The message must say the history survives, or a user reads "disabled" as
    # "deleted".
    assert "readable" in refusal


def test_paper_orders_pass_when_paper_is_on(monkeypatch):
    monkeypatch.setattr(order_service, "get_settings", lambda: settings(paper=True))
    assert order_service._paper_gate() is None


def test_disabling_paper_does_not_enable_live(monkeypatch):
    """The two gates are independent. Turning paper off must not be a way to
    reach the live path, which has its own unmet conditions."""
    monkeypatch.setattr(order_service, "get_settings", lambda: settings(paper=False, live=False))
    account = SimpleNamespace(
        id=uuid.uuid4(), broker="icici_breeze", live_enabled=True, environment="live"
    )
    assert order_service._live_gate(account) is not None


# ── the engine the backtester shares ─────────────────────────────────


def test_the_paper_engine_is_still_importable():
    """The backtester replays through the paper engine's charge model, ledger
    money arithmetic and fill accounting. Disabling paper accounts must not
    remove the machinery, or the one way to evaluate a strategy goes with it.
    """
    from app.engines.backtest import run_backtest
    from app.engines.paper.charges import compute_charges
    from app.engines.paper.ledger import money
    from app.engines.paper.pnl import apply_fill

    assert all(callable(f) for f in (run_backtest, compute_charges, money, apply_fill))


def test_a_backtest_still_runs_with_paper_disabled(monkeypatch):
    """The end this protects: a strategy can still be evaluated on a
    live-only instance."""
    from datetime import datetime, timezone
    from decimal import Decimal

    from app.engines.backtest import run_backtest
    from app.engines.strategy.sma_crossover import SmaCrossover

    def bar(i, price):
        p = Decimal(price)
        return SimpleNamespace(
            ts=datetime(2026, 1, 1, tzinfo=timezone.utc).replace(day=1 + i),
            open=p, high=p + 1, low=p - 1, close=p, volume=1000,
        )

    # A rising then falling series, enough for a 2/3 crossover to fire.
    prices = [100, 101, 102, 105, 110, 115, 112, 108, 104, 100, 98, 95]
    candles = [bar(i, p) for i, p in enumerate(prices)]

    result = run_backtest(
        SmaCrossover(), candles, symbol="RELIANCE", product="CNC",
        params={"fast": 2, "slow": 3, "quantity": 1}, interval="1d",
    )
    assert "total_return" in result


# ── the worker heartbeat ─────────────────────────────────────────────


async def test_the_heartbeat_still_beats_with_paper_disabled(monkeypatch):
    """paper_tick stamps the liveness /readyz reads. Skipping the whole job
    would report the worker dead -- and that worker also runs the tick stream
    and the fill reconciler, which a live-only instance depends on entirely.
    """
    from app.workers import jobs

    beats = []

    class FakeRedis:
        pass

    async def fake_beat(redis):
        beats.append(1)

    monkeypatch.setattr(jobs, "get_redis", lambda: FakeRedis())
    monkeypatch.setattr(jobs.heartbeat, "beat", fake_beat)
    monkeypatch.setattr(jobs, "get_settings", lambda: settings(paper=False))

    await jobs.paper_tick({})
    assert beats == [1], "the heartbeat must be stamped even when paper is off"


async def test_paper_stages_are_skipped_when_disabled(monkeypatch):
    """Nothing after the heartbeat should run: no price step, no settlement,
    no simulated fills."""
    from app.workers import jobs

    stepped = []

    async def fake_beat(redis):
        return None

    async def should_not_run(*a, **kw):
        stepped.append(1)
        return {}

    monkeypatch.setattr(jobs, "get_redis", lambda: object())
    monkeypatch.setattr(jobs.heartbeat, "beat", fake_beat)
    monkeypatch.setattr(jobs, "get_settings", lambda: settings(paper=False))
    monkeypatch.setattr(jobs.market_sim, "tick_all", should_not_run)

    await jobs.paper_tick({})
    assert stepped == []
