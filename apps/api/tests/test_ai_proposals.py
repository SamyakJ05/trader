"""AI proposal validation + analyst tool-loop orchestration. Pure-unit:
LLM and tool execution are stubbed; no DB or network."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.ai.analyst as analyst_module
from app.services.ai.analyst import chat
from app.services.ai.llm import LLMReply, LLMToolCall
from app.services.ai.tools import ToolError, validate_proposal_args

# ── propose_trade argument validation ────────────────────────────────


def test_valid_args_normalized():
    out = validate_proposal_args(
        {"symbol": " reliance ", "side": "buy", "quantity": "5", "rationale": "momentum"}
    )
    assert out["symbol"] == "RELIANCE"
    assert out["side"] == "BUY"
    assert out["quantity"] == 5
    assert out["order_type"] == "MARKET"
    assert out["product"] == "MIS"
    assert out["limit_price"] is None


def test_rejects_bad_side():
    with pytest.raises(ToolError, match="side"):
        validate_proposal_args(
            {"symbol": "TCS", "side": "SHORT", "quantity": 1, "rationale": "x"}
        )


def test_rejects_zero_quantity():
    with pytest.raises(ToolError, match="quantity"):
        validate_proposal_args({"symbol": "TCS", "side": "SELL", "quantity": 0, "rationale": "x"})


def test_limit_order_requires_price():
    with pytest.raises(ToolError, match="limit_price"):
        validate_proposal_args(
            {"symbol": "TCS", "side": "BUY", "quantity": 1, "order_type": "LIMIT", "rationale": "x"}
        )


def test_rejects_missing_rationale():
    with pytest.raises(ToolError, match="rationale"):
        validate_proposal_args({"symbol": "TCS", "side": "BUY", "quantity": 1})


# ── analyst tool loop ────────────────────────────────────────────────


class ScriptedLLM:
    """Returns queued replies; records the neutral conversations it was given."""

    provider = "anthropic"
    model = "test-model"

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    async def chat(self, system, messages, tools):
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0)

    async def generate_json(self, system, prompt, schema):  # pragma: no cover
        raise AssertionError("not used")


def account_stub():
    return SimpleNamespace(id=uuid.uuid4(), environment="paper", broker="paper")


async def test_plain_answer_no_tools(monkeypatch):
    llm = ScriptedLLM([LLMReply(text="Your paper account is flat.")])
    result = await chat(None, None, llm, uuid.uuid4(), account_stub(), [
        {"role": "user", "content": "any positions?"}
    ])
    assert result == {"reply": "Your paper account is flat.", "proposal_ids": []}


async def test_tool_round_then_answer(monkeypatch):
    proposal_id = str(uuid.uuid4())
    llm = ScriptedLLM(
        [
            LLMReply(
                text="Proposing.",
                tool_calls=[
                    LLMToolCall(id="t1", name="propose_trade", args={"symbol": "TCS"})
                ],
            ),
            LLMReply(text="Done — proposal awaits your approval."),
        ]
    )
    run_tool = AsyncMock(return_value=json.dumps({"proposal_id": proposal_id}))
    monkeypatch.setattr(analyst_module, "run_tool", run_tool)

    result = await chat(None, None, llm, uuid.uuid4(), account_stub(), [
        {"role": "user", "content": "buy some TCS"}
    ])

    assert result["reply"] == "Done — proposal awaits your approval."
    assert result["proposal_ids"] == [proposal_id]
    # second LLM call saw the assistant tool turn + tool results appended
    roles = [m["role"] for m in llm.seen[1]]
    assert roles == ["user", "assistant", "tool_results"]


async def test_tool_error_is_fed_back_not_raised(monkeypatch):
    llm = ScriptedLLM(
        [
            LLMReply(
                text="",
                tool_calls=[LLMToolCall(id="t1", name="propose_trade", args={})],
            ),
            LLMReply(text="That trade was invalid."),
        ]
    )
    run_tool = AsyncMock(side_effect=ToolError("quantity must be >= 1"))
    monkeypatch.setattr(analyst_module, "run_tool", run_tool)

    result = await chat(None, None, llm, uuid.uuid4(), account_stub(), [
        {"role": "user", "content": "buy"}
    ])
    assert result["reply"] == "That trade was invalid."
    assert result["proposal_ids"] == []
    error_payload = llm.seen[1][-1]["results"][0]["content"]
    assert "quantity" in error_payload
