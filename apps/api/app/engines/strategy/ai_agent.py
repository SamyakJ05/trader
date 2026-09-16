"""Autonomous AI strategy — an LLM decides BUY/SELL/HOLD per symbol on each
evaluation. Decisions become ordinary signals: the runner routes them through
the risk engine, kill switches, and the standard order pipeline like any other
strategy. Paper-only; guarded by a per-symbol interval and a daily decision cap.

params:
  instructions: str            plain-English trading instructions for the model
  quantity: int = 1            default share count per decision
  min_interval_seconds: int = 300   minimum gap between LLM calls per symbol
  max_decisions_per_day: int = 10   LLM-call budget per strategy per day
"""

import uuid
from datetime import date
from decimal import Decimal

from app.core.logging import get_logger
from app.domain.enums import AuditEventType, SignalType
from app.engines.strategy.base import AsyncStrategyBase, Signal, StrategyContext
from app.services import audit
from app.services.ai.llm import LLMError, resolve_llm

logger = get_logger(__name__)

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
        "quantity": {"type": "integer", "minimum": 0},
        "reason": {"type": "string"},
    },
    "required": ["action", "quantity", "reason"],
    "additionalProperties": False,
}

DECISION_SYSTEM = """You are an autonomous trading agent for Indian markets. \
Each call you receive one symbol's recent prices, the current position, and the \
operator's instructions. Decide BUY, SELL, or HOLD.

- HOLD with quantity 0 when there is no clear edge — that is the default.
- Never exceed the suggested quantity.
- reason: one sentence, it is audited."""

# Appended per evaluation. The static prompt asserted "a PAPER (simulated)
# account" unconditionally, which stopped being true once live trading was
# built -- and this strategy places orders with NO human in the loop, so a
# model wrongly believing its mistakes are free is the worst place for that
# claim to be stale.
_LIVE_NOTE = (
    "\n\nThis account is LIVE: your decision places a REAL order with REAL "
    "money and a fill cannot be undone. There is no human approval step. When "
    "in doubt, HOLD."
)
_PAPER_NOTE = (
    "\n\nThis account is PAPER: fills are simulated and no real money moves."
)


def map_decision(
    action: str, quantity: int, position_quantity: int, symbol: str, reason: str
) -> list[Signal]:
    """Map a model decision onto signal types relative to the current position."""
    if action == "HOLD" or quantity <= 0:
        return []
    if action == "BUY":
        signal_type = SignalType.EXIT_SHORT if position_quantity < 0 else SignalType.ENTRY_LONG
    elif action == "SELL":
        signal_type = SignalType.EXIT_LONG if position_quantity > 0 else SignalType.ENTRY_SHORT
    else:
        return []
    return [Signal(symbol=symbol, signal_type=signal_type, quantity=quantity, note=reason[:500])]


def _price_context(prices: list) -> str:
    """Summary statistics a human would read off a chart at a glance.

    Computed rather than left to the model: asking an LLM to derive a moving
    average from numbers in a prompt is asking it to do arithmetic it is
    unreliable at, and a wrong average silently becomes a wrong trade.
    """
    if len(prices) < 2:
        return "Price context: not enough history."
    values = [Decimal(str(p)) for p in prices]
    last = values[-1]
    window = values[-20:]
    average = sum(window) / len(window)
    high, low = max(window), min(window)
    change = (last - values[0]) / values[0] * 100 if values[0] else Decimal(0)

    def pct(value: Decimal) -> str:
        return f"{value.quantize(Decimal('0.01'))}%"

    return (
        f"Price context: last {last}, {len(window)}-period average "
        f"{average.quantize(Decimal('0.01'))} "
        f"({'above' if last > average else 'below'} it), "
        f"range {low}-{high} over that window, "
        f"{pct(change)} across the {len(values)} periods shown."
    )


class AiAgentStrategy(AsyncStrategyBase):
    kind = "ai_agent"

    def min_history(self, params: dict) -> int:
        return int(params.get("min_history", 20))

    async def evaluate_async(self, ctx: StrategyContext, *, db, redis) -> list[Signal]:
        params = ctx.params
        strategy_id = params.get("_strategy_id")
        user_id = params.get("_user_id")
        if not strategy_id or not user_id:
            return []

        llm = await resolve_llm(db, uuid.UUID(user_id))
        if llm is None:
            logger.warning("ai_agent_unconfigured", strategy=strategy_id)
            return []

        interval = max(int(params.get("min_interval_seconds", 300)), 60)
        last_key = f"ai_agent:last:{strategy_id}:{ctx.symbol}"
        if await redis.get(last_key):
            return []

        cap = int(params.get("max_decisions_per_day", 10))
        day_key = f"ai_agent:count:{strategy_id}:{date.today().isoformat()}"
        if int(await redis.get(day_key) or 0) >= cap:
            return []

        prices = [str(p) for p in ctx.prices[-30:]]
        is_live = str(params.get("_environment", "paper")) == "live"

        # A bare price list asks the model to be a chart reader on numbers in
        # a prompt, which is what it is worst at. These are the same summary
        # statistics a human would glance at first, computed exactly rather
        # than left to the model to infer from a comma-separated list.
        prompt = (
            f"Operator instructions: {params.get('instructions', 'No specific instructions.')}\n"
            f"Symbol: {ctx.symbol}\n"
            f"Recent prices (oldest to newest): {', '.join(prices)}\n"
            f"{_price_context(ctx.prices)}\n"
            f"Current position: {ctx.position_quantity} shares\n"
            f"Suggested quantity per trade: {int(params.get('quantity', 1))}"
        )
        system = DECISION_SYSTEM + (_LIVE_NOTE if is_live else _PAPER_NOTE)

        try:
            decision = await llm.generate_json(system, prompt, DECISION_SCHEMA)
        except LLMError as e:
            # Transient provider failure — skip this tick, don't error the strategy.
            logger.warning("ai_agent_llm_error", strategy=strategy_id, error=str(e))
            return []

        await redis.set(last_key, "1", ex=interval)
        await redis.incr(day_key)
        await redis.expire(day_key, 86400)

        action = str(decision.get("action", "HOLD")).upper()
        quantity = min(int(decision.get("quantity", 0)), int(params.get("quantity", 1)))
        reason = str(decision.get("reason", ""))

        await audit.emit(
            db,
            AuditEventType.AI_DECISION,
            user_id=uuid.UUID(user_id),
            entity_type="strategy",
            entity_id=uuid.UUID(strategy_id),
            payload={
                "symbol": ctx.symbol,
                "action": action,
                "quantity": quantity,
                "reason": reason,
                "provider": llm.provider,
                "model": llm.model,
            },
        )
        await db.commit()

        return map_decision(action, quantity, ctx.position_quantity, ctx.symbol, reason)
