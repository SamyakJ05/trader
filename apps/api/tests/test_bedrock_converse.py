"""Every non-Anthropic model AWS hosts, through Bedrock's Converse API.

The Anthropic SDK reaches Claude on Bedrock and nothing else: Nova, Llama,
Mistral and Cohere do not speak the Messages API. Converse is AWS's uniform
interface across all of them, which matters when the spend runs on AWS
credits rather than a separate vendor's billing.

The risk in a third provider is the message translation. Each API names the
same concepts differently, and a wrong tool-result shape does not error --
the model simply never sees the tool output and answers from nothing.
"""

import pytest

from app.services.ai.bedrock_converse import BedrockConverseLLM, uses_converse
from app.services.ai.llm import LLMError

# ── routing ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("model,expected", [
    # Claude keeps the Messages API, which supports its tool use natively.
    ("anthropic.claude-opus-5", False),
    ("us.anthropic.claude-sonnet-4-20250514-v1:0", False),
    ("eu.anthropic.claude-3-5-sonnet-20241022-v2:0", False),
    # Everything else AWS hosts needs Converse.
    ("amazon.nova-pro-v1:0", True),
    ("meta.llama3-3-70b-instruct-v1:0", True),
    ("mistral.mistral-large-2407-v1:0", True),
    ("cohere.command-r-plus-v1:0", True),
    ("ai21.jamba-1-5-large-v1:0", True),
])
def test_the_model_id_decides_which_client_answers(model, expected):
    """Matched by prefix rather than an exact list: AWS versions ids heavily
    and adds models faster than a hardcoded set would track."""
    assert uses_converse(model) is expected


# ── message translation, where this breaks silently ──────────────────


def test_a_tool_result_reaches_the_model():
    """Converse names this differently from both other providers. Get it
    wrong and nothing errors -- the model simply never sees the tool output
    and answers as though it had no data."""
    native = BedrockConverseLLM._to_native([
        {"role": "tool_results", "results": [{"id": "t1", "content": '{"cash": 1956}'}]}
    ])
    assert native[0]["role"] == "user"
    block = native[0]["content"][0]["toolResult"]
    assert block["toolUseId"] == "t1"
    assert "1956" in block["content"][0]["text"]


def test_an_assistant_tool_call_is_translated():
    native = BedrockConverseLLM._to_native([
        {
            "role": "assistant",
            "content": "checking funds",
            "tool_calls": [{"id": "t1", "name": "get_funds", "args": {}}],
        }
    ])
    content = native[0]["content"]
    assert content[0]["text"] == "checking funds"
    assert content[1]["toolUse"]["name"] == "get_funds"
    assert content[1]["toolUse"]["toolUseId"] == "t1"


def test_an_empty_message_is_dropped_rather_than_sent():
    """Converse rejects an empty content list outright, which would fail the
    whole turn over a message that carried nothing anyway."""
    native = BedrockConverseLLM._to_native([
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "user", "content": "hello"},
    ])
    assert len(native) == 1
    assert native[0]["content"][0]["text"] == "hello"


def test_tools_are_translated_to_a_toolspec():
    config = BedrockConverseLLM._to_native_tools([
        {
            "name": "get_positions",
            "description": "Current positions.",
            "input_schema": {"type": "object", "properties": {}},
        }
    ])
    spec = config["tools"][0]["toolSpec"]
    assert spec["name"] == "get_positions"
    assert spec["inputSchema"]["json"]["type"] == "object"


# ── responses ────────────────────────────────────────────────────────


async def test_a_reply_with_a_tool_call_is_parsed(monkeypatch):
    client = BedrockConverseLLM.__new__(BedrockConverseLLM)
    client.provider, client.model = "bedrock", "amazon.nova-pro-v1:0"

    async def fake(**kwargs):
        return {
            "output": {
                "message": {
                    "content": [
                        {"text": "Checking."},
                        {
                            "toolUse": {
                                "toolUseId": "t1",
                                "name": "get_funds",
                                "input": {},
                            }
                        },
                    ]
                }
            }
        }

    client._converse = fake
    reply = await client.chat("system", [{"role": "user", "content": "funds?"}], [])
    assert reply.text == "Checking."
    assert reply.tool_calls[0].name == "get_funds"
    assert reply.tool_calls[0].id == "t1"


async def test_structured_output_is_parsed_from_text():
    """Converse has no universal JSON mode -- support varies by model and
    several have none -- so the schema goes in the prompt and the reply is
    parsed."""
    client = BedrockConverseLLM.__new__(BedrockConverseLLM)
    client.provider, client.model = "bedrock", "meta.llama3-3-70b-instruct-v1:0"

    async def fake(**kwargs):
        return {"output": {"message": {"content": [
            {"text": '```json\n{"action": "HOLD", "quantity": 0}\n```'}
        ]}}}

    client._converse = fake
    out = await client.generate_json("sys", "decide", {"type": "object"})
    assert out == {"action": "HOLD", "quantity": 0}


async def test_prose_instead_of_json_is_an_error_not_a_guess():
    """A strategy acting on a half-parsed decision is worse than one that
    skips a tick."""
    client = BedrockConverseLLM.__new__(BedrockConverseLLM)
    client.provider, client.model = "bedrock", "amazon.nova-lite-v1:0"

    async def fake(**kwargs):
        return {"output": {"message": {"content": [
            {"text": "I think you should probably hold for now."}
        ]}}}

    client._converse = fake
    with pytest.raises(LLMError):
        await client.generate_json("sys", "decide", {"type": "object"})
