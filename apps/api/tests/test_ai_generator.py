"""Strategy generator draft validation + LLM plumbing. Pure-unit."""

import pytest

from app.services.ai.generator import draft_schema, generate, validate_draft
from app.services.ai.llm import LLMError, _parse_json_text, tools_to_openai


class StubLLM:
    provider = "openai"
    model = "test"

    def __init__(self, draft):
        self.draft = draft

    async def chat(self, system, messages, tools):  # pragma: no cover
        raise AssertionError("not used")

    async def generate_json(self, system, prompt, schema):
        return self.draft


def good_draft():
    return {
        "name": "Reliance momentum",
        "kind": "sma_crossover",
        "symbols": ["reliance"],
        "params": {"fast": 5, "slow": 20, "quantity": 2},
        "rationale": "Classic crossover fits the idea.",
    }


def test_schema_enum_tracks_registry():
    from app.engines.strategy.runner import STRATEGY_REGISTRY

    assert set(draft_schema()["properties"]["kind"]["enum"]) == set(STRATEGY_REGISTRY)


def test_validate_draft_normalizes_symbols():
    out = validate_draft(good_draft())
    assert out["symbols"] == ["RELIANCE"]
    assert out["kind"] == "sma_crossover"


def test_validate_draft_rejects_unknown_kind():
    draft = good_draft() | {"kind": "moon_phase"}
    with pytest.raises(ValueError, match="moon_phase"):
        validate_draft(draft)


def test_validate_draft_rejects_empty_symbols():
    draft = good_draft() | {"symbols": []}
    with pytest.raises(ValueError, match="symbols"):
        validate_draft(draft)


async def test_generate_returns_validated_draft():
    out = await generate(StubLLM(good_draft()), "trade reliance on momentum")
    assert out["name"] == "Reliance momentum"


async def test_generate_wraps_bad_draft_in_llm_error():
    bad = good_draft() | {"kind": "nope"}
    with pytest.raises(LLMError, match="invalid strategy draft"):
        await generate(StubLLM(bad), "whatever")


# ── llm helpers ──────────────────────────────────────────────────────


def test_tools_to_openai_mapping():
    tools = [
        {"name": "f", "description": "d", "input_schema": {"type": "object", "properties": {}}}
    ]
    out = tools_to_openai(tools)
    assert out == [
        {
            "type": "function",
            "function": {"name": "f", "description": "d", "parameters": {"type": "object", "properties": {}}},
        }
    ]


def test_parse_json_text_handles_fences():
    assert _parse_json_text('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json_text('prefix {"a": 1} suffix') == {"a": 1}
    with pytest.raises(LLMError):
        _parse_json_text("no json here")
