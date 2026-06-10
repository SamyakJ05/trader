"""Strategy abstraction — STATUS: SCAFFOLD (working for paper, deliberately minimal).

A strategy is a pure-ish decision function: given recent prices and current
position, emit signals. It never places orders directly — the runner routes
signals through the risk engine and order pipeline."""

from abc import ABC, abstractmethod
from decimal import Decimal

from pydantic import BaseModel

from app.domain.enums import SignalType


class StrategyContext(BaseModel):
    symbol: str
    prices: list[Decimal]  # oldest -> newest closes from the feed
    position_quantity: int
    params: dict


class Signal(BaseModel):
    symbol: str
    signal_type: SignalType
    quantity: int
    note: str = ""


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
