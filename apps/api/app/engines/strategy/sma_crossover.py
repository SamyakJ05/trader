"""Example strategy: SMA crossover. Long-only for MVP.

Fast SMA crossing above slow SMA -> enter long; crossing below -> exit.
Exists to exercise the signal -> risk -> order -> fill pipeline, not to
make money."""

from decimal import Decimal

from app.domain.enums import SignalType
from app.engines.strategy.base import Signal, StrategyBase, StrategyContext


def sma(values: list[Decimal], window: int) -> Decimal:
    return sum(values[-window:]) / window


class SmaCrossover(StrategyBase):
    kind = "sma_crossover"

    def min_history(self, params: dict) -> int:
        return int(params.get("slow", 20)) + 1

    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        fast = int(ctx.params.get("fast", 5))
        slow = int(ctx.params.get("slow", 20))
        quantity = int(ctx.params.get("quantity", 1))
        if len(ctx.prices) < slow + 1:
            return []

        fast_now = sma(ctx.prices, fast)
        slow_now = sma(ctx.prices, slow)
        fast_prev = sma(ctx.prices[:-1], fast)
        slow_prev = sma(ctx.prices[:-1], slow)

        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now

        if crossed_up and ctx.position_quantity == 0:
            return [
                Signal(
                    symbol=ctx.symbol,
                    signal_type=SignalType.ENTRY_LONG,
                    quantity=quantity,
                    note=f"fast {fast_now:.2f} crossed above slow {slow_now:.2f}",
                )
            ]
        if crossed_down and ctx.position_quantity > 0:
            return [
                Signal(
                    symbol=ctx.symbol,
                    signal_type=SignalType.EXIT_LONG,
                    quantity=ctx.position_quantity,
                    note=f"fast {fast_now:.2f} crossed below slow {slow_now:.2f}",
                )
            ]
        return []
