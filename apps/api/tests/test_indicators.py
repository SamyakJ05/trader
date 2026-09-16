"""Indicators checked against published values, not against themselves.

These decide order quantities on a live account with auto-trade enabled, so a
test that only asserts self-consistency ("RSI is between 0 and 100") would
pass on a wrong formula. The RSI and ATR cases below use Wilder's own worked
example from New Concepts in Technical Trading Systems (1978), which is the
reference every charting package is calibrated against.
"""

from decimal import Decimal
from itertools import pairwise

import pytest

from app.engines.strategy.base import Bar
from app.engines.strategy.indicators import (
    atr,
    bollinger,
    ema,
    macd,
    position_size_from_atr,
    rsi,
    sma,
    true_range,
)


def D(x) -> Decimal:
    return Decimal(str(x))


# Wilder's worked RSI example (New Concepts, 1978). The first 15 closes give
# the SEED reading -- 14 changes, consumed entirely by the opening average,
# with no smoothing applied yet. The published 66.25 and 66.48 are the next
# two bars, and those are the ones that exercise Wilder's smoothing, so they
# are the stronger assertion.
WILDER_CLOSES = [
    D(c)
    for c in [
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
        45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
    ]
]


def test_rsi_matches_wilders_published_smoothed_values():
    """The two readings after the seed, where smoothing actually applies.

    A formula that used a simple mean instead of Wilder's smoothing passes a
    seed-only check and diverges from here on, which is the error this pins.
    """
    assert rsi([*WILDER_CLOSES, D("46.00")]).quantize(D("0.01")) == D("66.25")
    assert rsi([*WILDER_CLOSES, D("46.00"), D("46.03")]).quantize(D("0.01")) == D(
        "66.48"
    )


def test_rsi_is_100_when_nothing_ever_falls():
    """avg_loss is zero here, and the naive formula divides by it. A momentum
    strategy evaluates precisely during a straight-up run, so this is a live
    path, not an edge case."""
    assert rsi([D(i) for i in range(1, 30)]) == D(100)


def test_rsi_uses_wilder_smoothing_not_a_simple_mean():
    """A simple mean of gains and losses is a DIFFERENT indicator that crosses
    30/70 at different times. It would pass a range check and trade wrong."""
    series = [*WILDER_CLOSES, D("46.00")]
    value = rsi(series)
    simple_gains = []
    simple_losses = []
    for a, b in pairwise(series):
        simple_gains.append(max(b - a, D(0)))
        simple_losses.append(max(a - b, D(0)))
    mean_gain = sum(simple_gains) / len(simple_gains)
    mean_loss = sum(simple_losses) / len(simple_losses)
    naive = D(100) - D(100) / (D(1) + mean_gain / mean_loss)
    assert value.quantize(D("0.01")) != naive.quantize(D("0.01"))


def test_insufficient_history_returns_none_rather_than_a_number():
    """The whole safety property of this module: too little data must be
    distinguishable from a real reading, or a strategy sizes a real order
    off four bars of a fourteen-bar indicator."""
    assert rsi(WILDER_CLOSES[:5]) is None
    assert atr([], 14) is None
    assert macd([D(1)] * 10) is None
    assert bollinger([D(1)] * 5, window=20) is None
    assert sma([D(1)], 5) is None
    assert ema([D(1)], 5) is None


def test_sma_is_the_plain_mean():
    assert sma([D(1), D(2), D(3), D(4)], 4) == D("2.5")
    # Only the last `window` values.
    assert sma([D(99), D(1), D(2), D(3)], 3) == D(2)


def test_ema_is_seeded_with_an_sma():
    """Seeding from the first value instead makes early output depend on one
    arbitrary bar, so two feeds with different history disagree about the
    same moment."""
    values = [D(1), D(2), D(3), D(4), D(5)]
    # With window == len, an EMA has no values left to smooth and equals the
    # seed SMA exactly.
    assert ema(values, 5) == sma(values, 5)


def test_ema_weights_recent_values_more_than_sma_does():
    """Deliberately NOT a linear ramp. On a constant-slope series the EMA and
    the SMA coincide exactly, so a ramp would assert nothing about weighting.
    A late jump separates them."""
    jumped = [D(10)] * 15 + [D(50)] * 5
    assert ema(jumped, 10) > sma(jumped, 10)


def test_bollinger_bands_straddle_the_middle_symmetrically():
    closes = [D(10), D(12), D(11), D(13), D(12)]
    lower, middle, upper = bollinger(closes, window=5)
    assert middle == sma(closes, 5)
    # Quantized: Decimal ** Decimal("0.5") is correctly rounded rather than
    # exact, so the two half-widths agree to full paise but not necessarily in
    # the 28th significant digit. Paise is the unit orders are priced in.
    places = D("0.000001")
    assert (upper - middle).quantize(places) == (middle - lower).quantize(places)
    assert lower < middle < upper


def test_bollinger_has_zero_width_without_variance():
    flat = [D(50)] * 20
    lower, middle, upper = bollinger(flat)
    assert lower == middle == upper == D(50)


def test_true_range_extends_across_a_gap():
    """The reason plain high-low is not enough. A gap down opens below the
    prior close, and the range that matters spans the gap -- which is when a
    stop most needs to be wide. Overnight gaps are routine on NSE."""
    gapped = Bar(open=D(90), high=D(92), low=D(89), close=D(91))
    assert true_range(previous_close=D(100), bar=gapped) == D(11)  # 100 - 89
    # Without a gap it reduces to the plain range.
    inside = Bar(open=D(100), high=D(102), low=D(99), close=D(101))
    assert true_range(previous_close=D(100), bar=inside) == D(3)


def test_atr_of_constant_range_bars_is_that_range():
    bars = [Bar(open=D(10), high=D(11), low=D(9), close=D(10)) for _ in range(30)]
    assert atr(bars) == D(2)


def test_atr_rises_when_volatility_rises():
    calm = [Bar(open=D(100), high=D(101), low=D(99), close=D(100)) for _ in range(20)]
    wild = [Bar(open=D(100), high=D(110), low=D(90), close=D(100)) for _ in range(20)]
    assert atr(calm + wild) > atr(calm)


def test_position_size_shrinks_as_volatility_grows():
    """The point of ATR sizing: the same rupee risk buys fewer shares of a
    volatile stock. Fixed quantity risks several times more on a violent
    counter than a quiet one, on the same day, invisibly."""
    quiet = position_size_from_atr(risk_amount=D(10000), atr_value=D(5))
    volatile = position_size_from_atr(risk_amount=D(10000), atr_value=D(50))
    assert quiet > volatile
    # 10000 / (5 * 2)
    assert quiet == 1000


def test_position_size_rounds_down_never_up():
    """Rounding must only reduce exposure. Rounding up would breach the risk
    budget by design on every trade that does not divide evenly."""
    assert position_size_from_atr(risk_amount=D(100), atr_value=D(7)) == 7  # 100/14


def test_position_size_is_zero_when_one_share_exceeds_the_budget():
    """No trade is the correct answer, not one share anyway."""
    assert position_size_from_atr(risk_amount=D(5), atr_value=D(100)) == 0


@pytest.mark.parametrize("bad", [D(0), D(-1)])
def test_position_size_refuses_nonsense_volatility(bad):
    assert position_size_from_atr(risk_amount=D(10000), atr_value=bad) == 0


def test_macd_histogram_is_the_difference_of_the_two_lines():
    closes = [D(100 + i) for i in range(60)]
    line, signal, histogram = macd(closes)
    assert histogram == line - signal


def test_macd_is_positive_in_an_uptrend_and_negative_in_a_downtrend():
    up = [D(100 + i) for i in range(60)]
    down = [D(160 - i) for i in range(60)]
    assert macd(up)[0] > 0
    assert macd(down)[0] < 0
