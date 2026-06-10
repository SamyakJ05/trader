"""ai_agent strategy: decision→signal mapping and guard rails.
Pure-unit — fakeredis, stubbed LLM resolution and audit."""

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

import app.engines.strategy.ai_agent as ai_agent_module
from app.domain.enums import SignalType
from app.engines.strategy.ai_agent import AiAgentStrategy, map_decision
from app.engines.strategy.base import StrategyContext

STRATEGY_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())


def ctx(symbol="RELIANCE", position=0, **param_overrides):
    return StrategyContext(
        symbol=symbol,
        prices=[Decimal("100"), Decimal("101"), Decimal("102")],
        position_quantity=position,
        params={
            "_strategy_id": STRATEGY_ID,
            "_user_id": USER_ID,
            "quantity": 5,
            "min_interval_seconds": 60,
            **param_overrides,
        },
    )


# ── decision mapping ─────────────────────────────────────────────────


def test_buy_flat_enters_long():
    [signal] = map_decision("BUY", 3, 0, "TCS", "breakout")
    assert signal.signal_type == SignalType.ENTRY_LONG
    assert signal.quantity == 3


def test_sell_long_exits_long():
    [signal] = map_decision("SELL", 2, 4, "TCS", "take profit")
    assert signal.signal_type == SignalType.EXIT_LONG


def test_sell_flat_enters_short():
    [signal] = map_decision("SELL", 2, 0, "TCS", "breakdown")
    assert signal.signal_type == SignalType.ENTRY_SHORT


def test_buy_short_exits_short():
    [signal] = map_decision("BUY", 2, -2, "TCS", "cover")
    assert signal.signal_type == SignalType.EXIT_SHORT


def test_hold_and_zero_quantity_produce_nothing():
    assert map_decision("HOLD", 5, 0, "TCS", "") == []
    assert map_decision("BUY", 0, 0, "TCS", "") == []


# ── evaluate_async guards ────────────────────────────────────────────


class StubLLM:
    provider = "anthropic"
    model = "test"

    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    async def chat(self, system, messages, tools):  # pragma: no cover
        raise AssertionError("not used")

    async def generate_json(self, system, prompt, schema):
        self.calls += 1
        return self.decision


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def db():
    return SimpleNamespace(commit=AsyncMock())


def wire(monkeypatch, llm):
    monkeypatch.setattr(ai_agent_module, "resolve_llm", AsyncMock(return_value=llm))
    monkeypatch.setattr(ai_agent_module.audit, "emit", AsyncMock())


async def test_unconfigured_returns_no_signals(monkeypatch, redis):
    monkeypatch.setattr(ai_agent_module, "resolve_llm", AsyncMock(return_value=None))
    out = await AiAgentStrategy().evaluate_async(ctx(), db=None, redis=redis)
    assert out == []


async def test_buy_decision_becomes_entry_long(monkeypatch, redis, db):
    llm = StubLLM({"action": "BUY", "quantity": 3, "reason": "uptrend"})
    wire(monkeypatch, llm)
    [signal] = await AiAgentStrategy().evaluate_async(ctx(), db=db, redis=redis)
    assert signal.signal_type == SignalType.ENTRY_LONG
    assert signal.quantity == 3
    assert llm.calls == 1


async def test_quantity_capped_at_params_quantity(monkeypatch, redis, db):
    llm = StubLLM({"action": "BUY", "quantity": 50, "reason": "greedy"})
    wire(monkeypatch, llm)
    [signal] = await AiAgentStrategy().evaluate_async(ctx(), db=db, redis=redis)
    assert signal.quantity == 5  # params.quantity


async def test_interval_guard_blocks_second_call(monkeypatch, redis, db):
    llm = StubLLM({"action": "HOLD", "quantity": 0, "reason": "wait"})
    wire(monkeypatch, llm)
    strategy = AiAgentStrategy()
    await strategy.evaluate_async(ctx(), db=db, redis=redis)
    out = await strategy.evaluate_async(ctx(), db=db, redis=redis)
    assert out == []
    assert llm.calls == 1


async def test_daily_cap_blocks_after_budget_spent(monkeypatch, redis, db):
    llm = StubLLM({"action": "HOLD", "quantity": 0, "reason": "wait"})
    wire(monkeypatch, llm)
    strategy = AiAgentStrategy()
    await strategy.evaluate_async(ctx(symbol="TCS", max_decisions_per_day=1), db=db, redis=redis)
    out = await strategy.evaluate_async(
        ctx(symbol="INFY", max_decisions_per_day=1), db=db, redis=redis
    )
    assert out == []
    assert llm.calls == 1


async def test_llm_error_skips_tick_without_raising(monkeypatch, redis, db):
    from app.services.ai.llm import LLMError

    llm = StubLLM({})
    llm.generate_json = AsyncMock(side_effect=LLMError("rate limited"))
    wire(monkeypatch, llm)
    out = await AiAgentStrategy().evaluate_async(ctx(), db=db, redis=redis)
    assert out == []
