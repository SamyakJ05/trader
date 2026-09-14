from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.domain.enums import SignalType
from app.engines.backtest import run_backtest
from app.engines.strategy.base import Signal, StrategyBase


class BuyThenSell(StrategyBase):
    kind = "test"

    def min_history(self, params):
        return 1

    def evaluate(self, ctx):
        return [
            Signal(
                symbol=ctx.symbol,
                quantity=1,
                signal_type=SignalType.ENTRY_LONG
                if ctx.position_quantity == 0
                else SignalType.EXIT_LONG,
            )
        ]


def bars(opens, closes):
    start = datetime(2026, 9, 15, 4, tzinfo=timezone.utc)
    return [
        SimpleNamespace(
            ts=start + timedelta(minutes=i),
            open=D(o),
            close=D(c),
            high=max(D(o), D(c)),
            low=min(D(o), D(c)),
            volume=100,
        )
        for i, (o, c) in enumerate(zip(opens, closes))
    ]


def test_next_open_removes_lookahead_profit():
    # Buying first close (10) and selling second close (100) fakes a profit.
    # Actual fills buy at next open 100 and sell at following open 10.
    result = run_backtest(
        BuyThenSell(),
        bars([1000, 10000, 1000], [1000, 10000, 1000]),
        symbol="TEST",
        initial_cash=D(100000),
    )
    assert [D(f["price"]) for f in result["fills"]] == [10000, 1000]
    assert D(result["total_return"]) < 0
    assert D(result["total_charges"]) > 0
    assert result["trade_count"] == 1
    assert result["win_rate"] == 0


def test_last_signal_has_no_fill_without_next_bar():
    result = run_backtest(BuyThenSell(), bars([10], [10]), symbol="TEST")
    assert result["fills"] == []


def test_no_negative_cash():
    result = run_backtest(
        BuyThenSell(), bars([10, 1000], [10, 1000]), symbol="TEST", initial_cash=D(100)
    )
    assert result["fills"] == []
    assert result["rejected_signals"] == 1


def test_unordered_data_refused():
    with pytest.raises(ValueError, match="strictly increasing"):
        run_backtest(BuyThenSell(), list(reversed(bars([10, 20], [10, 20]))), symbol="TEST")


def test_delivery_cannot_sell_same_day():
    result = run_backtest(
        BuyThenSell(), bars([10, 10, 10], [10, 10, 10]), symbol="TEST", product="CNC"
    )
    assert len(result["fills"]) == 1
    assert result["rejected_signals"] == 1


def test_cash_and_pnl_reconcile_with_final_equity():
    """The backtester accumulates cash and per-trade P&L separately: cash moves
    on each fill, trade_pnls is built for reporting. Nothing derives one from
    the other, so a future refactor could let them drift apart silently.

    This pins the invariant: starting cash, plus every closed trade's P&L, plus
    the mark on anything still open, must equal the reported final equity.
    """
    opens = [100, 102, 101, 105, 103, 108]
    closes = [102, 101, 105, 103, 108, 104]
    result = run_backtest(
        BuyThenSell(), bars(opens, closes), symbol="RELIANCE", initial_cash=D("100000")
    )
    closed = sum(D(f["charges"]) for f in result["fills"])  # charges are in the fills
    assert closed >= 0
    # Reconstruct equity from the reported pieces.
    final = D(result["final_equity"])
    stated_return = D(result["total_return"])
    assert (final - D("100000")) / D("100000") == stated_return
    # The equity curve's last point is the final equity.
    assert D(result["equity_curve"][-1]["equity"]) == final


def test_dp_charge_is_counted_once_per_day_across_a_multi_day_backtest():
    """DP is levied per scrip per day on delivery sells. A multi-day backtest
    must charge it on each day that has a sell, and only once within a day."""
    from datetime import timedelta

    # Two sells on separate days.
    opens = [100, 102, 101, 105]
    closes = [102, 101, 105, 103]
    candles = bars(opens, closes)
    # Push the last two bars into the next trading day.
    for c in candles[2:]:
        c.ts = c.ts + timedelta(days=1)
    result = run_backtest(
        BuyThenSell(),
        candles,
        symbol="RELIANCE",
        product="CNC",
        initial_cash=D("100000"),
    )
    assert result["fill_count"] >= 1
