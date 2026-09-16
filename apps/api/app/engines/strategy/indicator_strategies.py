"""Four indicator strategies: RSI, MACD, Bollinger, ATR channel.

Long-only, matching sma_crossover and the rest of the platform: shorting cash
equity intraday is a different risk profile and a different product, and none
of the exit plumbing assumes a negative position.

Each strategy decides only WHAT and HOW MUCH. The runner routes every signal
through the risk engine and the order pipeline, so MAX_ORDER_NOTIONAL and the
rest still apply to anything emitted here -- these cannot bypass a limit.

Sizing: every strategy accepts a fixed `quantity`, and optionally `risk_amount`
to size from ATR instead. Volatility sizing is the better default but cannot be
the only one, because it needs bars the backtester may not supply.

An entry never doubles an existing position (all four check
position_quantity == 0). Without that, a strategy whose condition persists
across several bars -- RSI staying below 30 for a week is ordinary -- would add
on every single evaluation and build an unbounded position out of one signal.
"""

from decimal import Decimal

from app.domain.enums import SignalType
from app.engines.strategy.base import Signal, StrategyBase, StrategyContext
from app.engines.strategy.indicators import (
    atr,
    bollinger,
    macd,
    position_size_from_atr,
    rsi,
)


def _quantity(ctx: StrategyContext, params: dict) -> int:
    """Fixed quantity, or ATR-derived when risk_amount is configured.

    Falls back to the fixed quantity when ATR cannot be computed -- too few
    bars, or a backtest feed with no bars at all -- rather than refusing to
    trade or guessing a volatility figure.
    """
    risk_amount = params.get("risk_amount")
    if risk_amount:
        atr_value = atr(ctx.bars, int(params.get("atr_period", 14)))
        if atr_value is not None:
            sized = position_size_from_atr(
                risk_amount=Decimal(str(risk_amount)),
                atr_value=atr_value,
                atr_multiple=Decimal(str(params.get("atr_multiple", 2))),
            )
            # Zero means one share already exceeds the risk budget. That is a
            # decision not to trade, so it is returned as-is rather than
            # falling back to the fixed quantity and overriding it.
            return sized
    return int(params.get("quantity", 1))


class RsiMeanReversion(StrategyBase):
    """Buy oversold, sell back at the overbought or neutral line.

    Mean reversion: the assumption is that an extreme reading reverts. That
    assumption fails in a strong trend -- RSI can sit under 30 the whole way
    down -- which is what the ATR sizing and the platform's stop rules are for.
    """

    kind = "rsi_mean_reversion"

    def min_history(self, params: dict) -> int:
        # +1 because RSI needs `period` CHANGES, so period+1 closes.
        return int(params.get("period", 14)) + 1

    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        period = int(ctx.params.get("period", 14))
        oversold = Decimal(str(ctx.params.get("oversold", 30)))
        overbought = Decimal(str(ctx.params.get("overbought", 70)))

        value = rsi(ctx.prices, period)
        if value is None:
            return []

        if value <= oversold and ctx.position_quantity == 0:
            quantity = _quantity(ctx, ctx.params)
            if quantity <= 0:
                return []
            return [
                Signal(
                    symbol=ctx.symbol,
                    signal_type=SignalType.ENTRY_LONG,
                    quantity=quantity,
                    note=f"RSI {value:.2f} at or below oversold {oversold}",
                )
            ]
        if value >= overbought and ctx.position_quantity > 0:
            return [
                Signal(
                    symbol=ctx.symbol,
                    signal_type=SignalType.EXIT_LONG,
                    quantity=ctx.position_quantity,
                    note=f"RSI {value:.2f} at or above overbought {overbought}",
                )
            ]
        return []


class MacdCrossover(StrategyBase):
    """Enter when the MACD line crosses above its signal line, exit below.

    The CROSS is the event, not the sign: a histogram that is merely positive
    has been positive for a while, and entering on that buys a move already
    made. So both this bar and the previous one are computed.
    """

    kind = "macd_crossover"

    def min_history(self, params: dict) -> int:
        slow = int(params.get("slow", 26))
        signal = int(params.get("signal", 9))
        # +1 for the previous bar the cross is measured against.
        return slow + signal + 1

    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        fast = int(ctx.params.get("fast", 12))
        slow = int(ctx.params.get("slow", 26))
        signal_period = int(ctx.params.get("signal", 9))

        now = macd(ctx.prices, fast, slow, signal_period)
        before = macd(ctx.prices[:-1], fast, slow, signal_period)
        if now is None or before is None:
            return []

        macd_now, signal_now, _ = now
        macd_before, signal_before, _ = before
        crossed_up = macd_before <= signal_before and macd_now > signal_now
        crossed_down = macd_before >= signal_before and macd_now < signal_now

        if crossed_up and ctx.position_quantity == 0:
            quantity = _quantity(ctx, ctx.params)
            if quantity <= 0:
                return []
            return [
                Signal(
                    symbol=ctx.symbol,
                    signal_type=SignalType.ENTRY_LONG,
                    quantity=quantity,
                    note=f"MACD {macd_now:.4f} crossed above signal {signal_now:.4f}",
                )
            ]
        if crossed_down and ctx.position_quantity > 0:
            return [
                Signal(
                    symbol=ctx.symbol,
                    signal_type=SignalType.EXIT_LONG,
                    quantity=ctx.position_quantity,
                    note=f"MACD {macd_now:.4f} crossed below signal {signal_now:.4f}",
                )
            ]
        return []


class BollingerBreakout(StrategyBase):
    """Two opposite readings of the same bands, chosen by `mode`.

    mode="reversion" (default) buys the lower band expecting a snap back to
    the middle. mode="breakout" buys a close ABOVE the upper band, reading the
    same event as strength rather than excess.

    Both are legitimate and they are exact opposites, so the mode is explicit
    rather than inferred. A strategy that guessed would be right half the time
    with no way to tell which half.
    """

    kind = "bollinger_bands"

    def min_history(self, params: dict) -> int:
        return int(params.get("window", 20))

    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        window = int(ctx.params.get("window", 20))
        num_std = Decimal(str(ctx.params.get("num_std", 2)))
        mode = str(ctx.params.get("mode", "reversion")).lower()

        bands = bollinger(ctx.prices, window, num_std)
        if bands is None:
            return []
        lower, middle, upper = bands
        close = ctx.prices[-1]

        if mode == "breakout":
            entry = close > upper
            exit_ = close < middle
            entry_note = f"close {close} broke above upper band {upper:.2f}"
            exit_note = f"close {close} fell back through middle {middle:.2f}"
        else:
            entry = close < lower
            exit_ = close > middle
            entry_note = f"close {close} below lower band {lower:.2f}"
            exit_note = f"close {close} reverted above middle {middle:.2f}"

        if entry and ctx.position_quantity == 0:
            quantity = _quantity(ctx, ctx.params)
            if quantity <= 0:
                return []
            return [
                Signal(
                    symbol=ctx.symbol,
                    signal_type=SignalType.ENTRY_LONG,
                    quantity=quantity,
                    note=entry_note,
                )
            ]
        if exit_ and ctx.position_quantity > 0:
            return [
                Signal(
                    symbol=ctx.symbol,
                    signal_type=SignalType.EXIT_LONG,
                    quantity=ctx.position_quantity,
                    note=exit_note,
                )
            ]
        return []


class AtrChannelBreakout(StrategyBase):
    """Donchian-style channel breakout with an ATR trailing stop.

    The Turtle idea: enter on a new N-bar high, leave on a trailing stop set a
    multiple of ATR below the highest close since entry, rather than on a
    fixed percentage. A fixed stop is too tight for a violent instrument and
    too loose for a quiet one, and gets taken out by ordinary noise on the
    first.

    Needs bars, so it returns nothing when the feed supplies none. Deriving
    highs from closes would understate every range and place every stop too
    close -- worse than not trading.
    """

    kind = "atr_channel"

    def min_history(self, params: dict) -> int:
        entry_window = int(params.get("entry_window", 20))
        atr_period = int(params.get("atr_period", 14))
        return max(entry_window, atr_period + 1) + 1

    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        entry_window = int(ctx.params.get("entry_window", 20))
        atr_period = int(ctx.params.get("atr_period", 14))
        stop_multiple = Decimal(str(ctx.params.get("stop_multiple", 2)))

        if len(ctx.bars) < max(entry_window, atr_period + 1) + 1:
            return []
        atr_value = atr(ctx.bars, atr_period)
        if atr_value is None:
            return []

        close = ctx.bars[-1].close
        # Excludes the current bar: comparing a bar against a window that
        # contains it makes the highest bar trivially equal to itself, and
        # every new high would also be a breakout.
        prior = ctx.bars[-(entry_window + 1) : -1]
        highest = max(bar.high for bar in prior)

        if close > highest and ctx.position_quantity == 0:
            quantity = _quantity(ctx, ctx.params)
            if quantity <= 0:
                return []
            return [
                Signal(
                    symbol=ctx.symbol,
                    signal_type=SignalType.ENTRY_LONG,
                    quantity=quantity,
                    note=(
                        f"close {close} broke {entry_window}-bar high {highest} "
                        f"(ATR {atr_value:.2f})"
                    ),
                )
            ]

        if ctx.position_quantity > 0:
            # Trailed from the highest close in the window, not from the entry
            # price: the strategy does not know its own entry price, and a
            # stop anchored to a stale price stops trailing.
            peak = max(bar.close for bar in ctx.bars[-entry_window:])
            stop = peak - atr_value * stop_multiple
            if close < stop:
                return [
                    Signal(
                        symbol=ctx.symbol,
                        signal_type=SignalType.EXIT_LONG,
                        quantity=ctx.position_quantity,
                        note=(
                            f"close {close} below ATR trailing stop {stop:.2f} "
                            f"({stop_multiple}x ATR under peak {peak})"
                        ),
                    )
                ]
        return []
