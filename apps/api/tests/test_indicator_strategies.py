"""Behaviour of the four indicator strategies.

These run with auto-trade enabled, so a signal here becomes a real order
gated only by the risk rules. What is pinned is therefore not "a signal was
produced" but the conditions under which one must NOT be: no doubling into an
existing position, no entry on stale or absent data, no exit invented when
flat.
"""

from decimal import Decimal

import pytest

from app.domain.enums import SignalType
from app.engines.strategy.base import Bar, StrategyContext
from app.engines.strategy.indicator_strategies import (
    AtrChannelBreakout,
    BollingerBreakout,
    MacdCrossover,
    RsiMeanReversion,
)
from app.engines.strategy.runner import STRATEGY_REGISTRY

ALL_KINDS = [
    "rsi_mean_reversion",
    "macd_crossover",
    "bollinger_bands",
    "atr_channel",
]


def D(x) -> Decimal:
    return Decimal(str(x))


def ctx(prices, *, position=0, bars=None, **params) -> StrategyContext:
    return StrategyContext(
        symbol="RELIANCE",
        prices=[D(p) for p in prices],
        position_quantity=position,
        params=params,
        bars=bars or [],
    )


def flat_bars(closes, *, spread=1):
    """Bars whose close follows `closes`, with a constant range around it."""
    return [
        Bar(open=D(c), high=D(c + spread), low=D(c - spread), close=D(c))
        for c in closes
    ]


# A decline steep enough to drive RSI below 30, then a recovery above 70.
FALLING = [100 - i * 2 for i in range(20)]
RISING = [60 + i * 2 for i in range(20)]


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_every_new_strategy_is_registered(kind):
    """The UI lists registry keys, so registration is what makes a strategy
    reachable at all."""
    assert kind in STRATEGY_REGISTRY


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_no_strategy_signals_on_empty_history(kind):
    """Insufficient data must produce no signal rather than a signal computed
    from whatever happened to be there."""
    assert STRATEGY_REGISTRY[kind].evaluate(ctx([])) == []


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_no_strategy_exits_a_position_it_does_not_have(kind):
    """An EXIT_LONG while flat would place a sell order against no holding --
    a short, on a long-only strategy."""
    signals = STRATEGY_REGISTRY[kind].evaluate(
        ctx(RISING, position=0, bars=flat_bars(RISING))
    )
    assert all(s.signal_type != SignalType.EXIT_LONG for s in signals)


# ── RSI ──────────────────────────────────────────────────────────────


def test_rsi_enters_long_when_oversold():
    signals = RsiMeanReversion().evaluate(ctx(FALLING, quantity=10))
    assert [s.signal_type for s in signals] == [SignalType.ENTRY_LONG]
    assert signals[0].quantity == 10


def test_rsi_does_not_add_to_an_existing_position():
    """RSI can sit below 30 for many consecutive bars -- that is ordinary in a
    downtrend. Without this check each evaluation would buy again and build an
    unbounded position from a single signal."""
    assert RsiMeanReversion().evaluate(ctx(FALLING, position=10, quantity=10)) == []


def test_rsi_exits_the_whole_position_when_overbought():
    signals = RsiMeanReversion().evaluate(ctx(RISING, position=7, quantity=10))
    assert [s.signal_type for s in signals] == [SignalType.EXIT_LONG]
    # The held quantity, not the configured one: exiting `quantity` would
    # leave a remainder no later signal ever clears.
    assert signals[0].quantity == 7


def test_rsi_thresholds_are_configurable():
    """Proves the threshold is read rather than hardcoded at 30.

    A mixed series, not the monotonic decline: that one has RSI exactly 0,
    which sits below every threshold and so could not distinguish a parameter
    that works from one that is ignored.
    """
    choppy = [100, 98, 101, 97, 102, 96, 103, 95, 104, 94, 105, 93, 106, 92, 107, 91]
    strategy = RsiMeanReversion()
    # Its RSI is mid-range, so a default-threshold run does nothing...
    assert strategy.evaluate(ctx(choppy, quantity=10)) == []
    # ...while raising the threshold above that reading makes it an entry.
    signals = strategy.evaluate(ctx(choppy, oversold=99, quantity=10))
    assert [s.signal_type for s in signals] == [SignalType.ENTRY_LONG]


# ── MACD ─────────────────────────────────────────────────────────────


def test_macd_requires_a_cross_not_merely_a_positive_histogram():
    """A histogram positive for many bars is not an entry: the move has
    already happened. Only the bar where the lines cross should trade."""
    steady_up = [D(100 + i) for i in range(80)]
    strategy = MacdCrossover()
    # Deep into an established uptrend the lines no longer cross.
    late = strategy.evaluate(ctx(steady_up, quantity=5))
    assert late == []


def test_macd_does_not_add_to_an_existing_position():
    prices = [100 - i for i in range(40)] + [60 + i * 2 for i in range(40)]
    assert MacdCrossover().evaluate(ctx(prices, position=5, quantity=5)) == []


# ── Bollinger ────────────────────────────────────────────────────────


def test_bollinger_reversion_and_breakout_are_opposite_on_the_same_data():
    """The two modes read one event in opposite directions, which is why the
    mode is explicit. A close above the upper band is an entry for breakout
    and not for reversion."""
    prices = [D(100)] * 19 + [D(130)]
    entered_breakout = BollingerBreakout().evaluate(ctx(prices, mode="breakout", quantity=3))
    entered_reversion = BollingerBreakout().evaluate(ctx(prices, mode="reversion", quantity=3))
    assert [s.signal_type for s in entered_breakout] == [SignalType.ENTRY_LONG]
    assert entered_reversion == []


def test_bollinger_reversion_buys_the_lower_band():
    prices = [D(100)] * 19 + [D(70)]
    signals = BollingerBreakout().evaluate(ctx(prices, mode="reversion", quantity=3))
    assert [s.signal_type for s in signals] == [SignalType.ENTRY_LONG]


# ── ATR channel ──────────────────────────────────────────────────────


def test_atr_channel_needs_bars_and_will_not_infer_them_from_closes():
    """Deriving a high from a close understates every range and places every
    stop too close. No bars must mean no trade."""
    breakout = [100] * 25 + [200]
    assert AtrChannelBreakout().evaluate(ctx(breakout, quantity=2)) == []


def test_atr_channel_enters_on_a_new_high():
    closes = [100] * 25 + [200]
    signals = AtrChannelBreakout().evaluate(
        ctx(closes, bars=flat_bars(closes), quantity=2)
    )
    assert [s.signal_type for s in signals] == [SignalType.ENTRY_LONG]


def test_atr_channel_does_not_enter_without_a_breakout():
    closes = [100] * 30
    assert (
        AtrChannelBreakout().evaluate(ctx(closes, bars=flat_bars(closes), quantity=2))
        == []
    )


def test_atr_channel_exits_when_price_falls_through_the_trailing_stop():
    # Rise to a peak, then collapse far enough to breach a 2-ATR trail.
    closes = [100] * 20 + [150] * 5 + [60]
    signals = AtrChannelBreakout().evaluate(
        ctx(closes, position=4, bars=flat_bars(closes), quantity=2)
    )
    assert [s.signal_type for s in signals] == [SignalType.EXIT_LONG]
    assert signals[0].quantity == 4


# ── ATR sizing ───────────────────────────────────────────────────────


def test_risk_amount_sizes_the_order_from_volatility():
    """The sizing path: quantity comes from ATR, not from the fixed param."""
    calm = flat_bars(FALLING, spread=1)
    signals = RsiMeanReversion().evaluate(
        ctx(FALLING, bars=calm, risk_amount=10000, quantity=1)
    )
    assert signals[0].quantity > 1


def test_a_more_volatile_instrument_gets_a_smaller_order():
    """Same rupee risk, wider stop, fewer shares. Fixed quantity would risk
    several times more on the volatile one with no indication."""
    quiet = RsiMeanReversion().evaluate(
        ctx(FALLING, bars=flat_bars(FALLING, spread=1), risk_amount=10000)
    )
    wild = RsiMeanReversion().evaluate(
        ctx(FALLING, bars=flat_bars(FALLING, spread=20), risk_amount=10000)
    )
    assert quiet[0].quantity > wild[0].quantity


def test_no_order_when_one_share_exceeds_the_risk_budget():
    """Zero is a decision not to trade and must not fall back to the fixed
    quantity, which would override the risk budget entirely."""
    assert (
        RsiMeanReversion().evaluate(
            ctx(FALLING, bars=flat_bars(FALLING, spread=50), risk_amount=1, quantity=99)
        )
        == []
    )


def test_sizing_falls_back_to_fixed_quantity_without_bars():
    """A backtest feed supplies no bars. Refusing to trade there would make
    the strategy untestable; guessing a volatility figure would be worse."""
    signals = RsiMeanReversion().evaluate(ctx(FALLING, risk_amount=10000, quantity=3))
    assert signals[0].quantity == 3
