"""Technical indicators, from their standard definitions.

Written from the published formulas (Wilder 1978 for RSI and ATR, Appel for
MACD, Bollinger for the bands) rather than ported from any implementation:
the formulas are public, particular codebases are not, and the one surveyed
for this work is GPL-3.0, which would have obliged this whole platform to be
GPL too.

Decimal throughout, never float. These numbers decide order quantities on a
real account, and binary floating point makes 0.1 + 0.2 != 0.3 -- an error
that is invisible in a chart and material in a position size.

Every function returns None rather than a wrong answer when it has too little
history. A caller that treats None as "no signal" degrades safely; one that
receives a number computed from four bars of a fourteen-bar indicator does
not, because it cannot tell that it did.
"""

from decimal import Decimal
from itertools import pairwise

from app.engines.strategy.base import Bar

# Wilder's original periods. Not tuning -- these are the values the published
# interpretation thresholds (RSI 30/70) were derived against, so changing one
# without the other silently changes what a signal means.
RSI_PERIOD = 14
ATR_PERIOD = 14


def sma(values: list[Decimal], window: int) -> Decimal | None:
    """Simple moving average of the last `window` values."""
    if window <= 0 or len(values) < window:
        return None
    return sum(values[-window:]) / window


def ema(values: list[Decimal], window: int) -> Decimal | None:
    """Exponential moving average, seeded with an SMA.

    Seeding matters: starting from the first value instead makes the early
    output depend on one arbitrary bar, and two feeds with different history
    lengths then disagree about the same moment in the market.
    """
    if window <= 0 or len(values) < window:
        return None
    multiplier = Decimal(2) / Decimal(window + 1)
    current = sum(values[:window]) / window
    for value in values[window:]:
        current = (value - current) * multiplier + current
    return current


def rsi(closes: list[Decimal], period: int = RSI_PERIOD) -> Decimal | None:
    """Wilder's Relative Strength Index, 0-100.

    Uses Wilder's smoothing (an EMA with alpha = 1/period), not a simple mean
    of gains and losses. The simple-mean version is a different and more
    jagged indicator that crosses the 30/70 thresholds at different times, so
    the distinction is not academic.
    """
    if period <= 0 or len(closes) < period + 1:
        return None

    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for previous, current in pairwise(closes):
        change = current - previous
        gains.append(max(change, Decimal(0)))
        losses.append(max(-change, Decimal(0)))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    # No losses at all: RS is undefined (division by zero), and the limit is
    # 100. Returning it explicitly avoids an exception on a straight-up run,
    # which is exactly when a momentum strategy is evaluating.
    if avg_loss == 0:
        return Decimal(100)
    rs = avg_gain / avg_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))


def macd(
    closes: list[Decimal],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[Decimal, Decimal, Decimal] | None:
    """MACD line, signal line and histogram.

    The signal line is an EMA OF the MACD line, so the MACD line has to be
    computed at every step -- which is why this rebuilds the series rather
        than taking a single reading.
    """
    if slow <= fast or len(closes) < slow + signal:
        return None

    macd_series: list[Decimal] = []
    for end in range(slow, len(closes) + 1):
        window = closes[:end]
        fast_ema = ema(window, fast)
        slow_ema = ema(window, slow)
        if fast_ema is None or slow_ema is None:
            return None
        macd_series.append(fast_ema - slow_ema)

    signal_line = ema(macd_series, signal)
    if signal_line is None:
        return None
    macd_line = macd_series[-1]
    return macd_line, signal_line, macd_line - signal_line


def bollinger(
    closes: list[Decimal],
    window: int = 20,
    num_std: Decimal = Decimal(2),
) -> tuple[Decimal, Decimal, Decimal] | None:
    """Lower band, middle (SMA) and upper band."""
    if len(closes) < window:
        return None
    middle = sum(closes[-window:]) / window
    variance = sum((c - middle) ** 2 for c in closes[-window:]) / window
    # Decimal has no sqrt operator; ** Decimal("0.5") uses the correctly
    # rounded decimal power rather than converting through float.
    deviation = variance ** Decimal("0.5")
    return middle - num_std * deviation, middle, middle + num_std * deviation


def true_range(previous_close: Decimal, bar: Bar) -> Decimal:
    """Wilder's true range: the day's range, extended across any gap.

    The plain high-low range understates a gap open, which is precisely when
    a stop needs to be widest. On Indian equities gaps are routine -- news and
    global moves land overnight while the market is shut.
    """
    return max(
        bar.high - bar.low,
        abs(bar.high - previous_close),
        abs(bar.low - previous_close),
    )


def atr(bars: list[Bar], period: int = ATR_PERIOD) -> Decimal | None:
    """Average True Range, Wilder-smoothed.

    The volatility figure position sizing is derived from: a stop placed at a
    fixed percentage treats a quiet counter and a violent one identically,
    and gets hit by noise on the second.
    """
    if period <= 0 or len(bars) < period + 1:
        return None

    ranges = [
        true_range(previous.close, current)
        for previous, current in pairwise(bars)
    ]
    current = sum(ranges[:period]) / period
    for value in ranges[period:]:
        current = (current * (period - 1) + value) / period
    return current


def position_size_from_atr(
    *,
    risk_amount: Decimal,
    atr_value: Decimal,
    atr_multiple: Decimal = Decimal(2),
) -> int:
    """Shares to buy so that an `atr_multiple`-ATR move loses `risk_amount`.

    Volatility-scaled sizing: a wider stop buys fewer shares, so the rupee
    risk per trade is the same whichever instrument it is. Fixed quantity
    does the opposite -- the same 100 shares risks several times more on a
    volatile counter than a quiet one, on the same day, with no indication
    that anything differs.

    Floor division, so rounding can only ever reduce exposure. Returns 0 when
    even one share would exceed the risk budget: no trade is the correct
    answer there, not a smaller-than-possible one.
    """
    if atr_value <= 0 or atr_multiple <= 0 or risk_amount <= 0:
        return 0
    stop_distance = atr_value * atr_multiple
    return int(risk_amount / stop_distance)
