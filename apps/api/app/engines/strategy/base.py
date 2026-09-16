"""Strategy abstraction — STATUS: SCAFFOLD (working for paper, deliberately minimal).

A strategy is a pure-ish decision function: given recent prices and current
position, emit signals. It never places orders directly — the runner routes
signals through the risk engine and order pipeline."""

from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from app.domain.enums import OptionRight, OrderType, ProductType, SignalType


class StrategyContext(BaseModel):
    symbol: str
    prices: list[Decimal]  # oldest -> newest closes from the feed
    position_quantity: int
    params: dict


class Signal(BaseModel):
    """What a strategy decided, including how it wants the order shaped.

    Execution intent belongs to the strategy, not to the platform. A momentum
    entry that must not be missed and a patient mean-reversion exit want
    different aggression, and a single platform-wide buffer would serve one
    badly to serve the other. Every field below is optional: a strategy that
    says nothing gets the account's defaults, which is what the simple ones
    do.
    """

    symbol: str
    signal_type: SignalType
    quantity: int
    note: str = ""

    # None means "let the runner decide from the account and params". A
    # strategy that names a type gets exactly that type.
    order_type: OrderType | None = None
    product: ProductType | None = None

    # An explicit price, when the strategy has computed one itself.
    limit_price: Decimal | None = None

    # How far through the last traded price to place a marketable limit, as a
    # fraction (0.003 = 0.30%). Used only when order_type is MARKET or LIMIT
    # and no limit_price was given. Buy limits sit above the last price and
    # sell limits below, so the order crosses the spread and fills rather than
    # resting. Bounded by MAX_LIMIT_BUFFER_PCT: a strategy asking to pay 50%
    # through the book has a bug, and the platform should not execute a bug.
    limit_buffer_pct: Decimal | None = None

    # For stop-loss orders.
    trigger_price: Decimal | None = None

    # Derivatives contract, when the signal names one. A symbol alone does
    # not identify an F&O contract -- Breeze lists 3,350 NIFTY contracts
    # under that one code -- so a strategy trading options must say which.
    # Falls back to the strategy's params, so a single-contract strategy can
    # configure it once rather than repeating it on every signal.
    expiry: date | None = None
    strike: Decimal | None = None
    right: OptionRight | None = None


class StrategyBase(ABC):
    kind: str  # registry key stored on the strategies row

    @abstractmethod
    def min_history(self, params: dict) -> int:
        """Number of price points needed before evaluation makes sense."""

    @abstractmethod
    def evaluate(self, ctx: StrategyContext) -> list[Signal]: ...


class AsyncStrategyBase(StrategyBase):
    """Strategy that needs awaitable resources (LLM calls, redis guards).
    The runner injects db/redis; sync evaluate() is intentionally unusable."""

    def evaluate(self, ctx: StrategyContext) -> list[Signal]:
        raise NotImplementedError(f"{self.kind} is async — use evaluate_async")

    @abstractmethod
    async def evaluate_async(self, ctx: StrategyContext, *, db, redis) -> list[Signal]: ...
