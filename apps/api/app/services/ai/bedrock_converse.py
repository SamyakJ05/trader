"""Bedrock's Converse API: every non-Anthropic model AWS hosts.

The Anthropic SDK reaches Claude on Bedrock and nothing else -- Nova, Llama,
Mistral, Cohere and the rest do not speak the Messages API. Converse is AWS's
own uniform interface across all of them, so one client covers every model an
account has access to, and the spend goes through AWS credits rather than a
separate vendor.

Implements the same two methods as the other clients (chat, generate_json) so
the analyst loop and the strategy agent need no knowledge of which provider
answered.

boto3 is synchronous. Every call goes through a thread rather than blocking
the event loop -- this runs inside the API process that also serves requests
and the worker that runs the tick.
"""

import asyncio
import json

from app.core.logging import get_logger
from app.services.ai.llm import LLMError, LLMReply, LLMToolCall, _parse_json_text

logger = get_logger(__name__)

_MAX_TOKENS = 4096

# Models reachable through Converse but NOT through the Anthropic SDK. Used to
# route a bedrock provider to the right client: an anthropic.* or us.anthropic.*
# id keeps the Messages API, everything else comes here.
#
# Matched by prefix because AWS versions ids heavily (amazon.nova-pro-v1:0) and
# adds models faster than any hardcoded list would track.
CONVERSE_PREFIXES = (
    "amazon.",       # Nova, Titan
    "meta.",         # Llama
    "mistral.",      # Mistral, Mixtral
    "cohere.",       # Command
    "ai21.",         # Jamba
    "openai.",       # GPT-6/GPT-5.6 and the gpt-oss line, hosted by AWS
    "deepseek.",
    "qwen.",
    "xai.",
    "minimax.",
    "moonshot.",
    "zai.",
    "nvidia.",
    "twelvelabs.",
    "writer.",
    "luma.",
    "stability.",
)

# Anything NOT Claude goes through Converse, prefix list or not. The list
# above is documentation of what AWS hosts today; this is the rule.
#
# A new vendor prefix appearing in Bedrock would otherwise route to the
# Anthropic SDK and fail on a request shape that model does not speak --
# which is what happened to openai.* before it was added here, and would
# happen again to the next one.
_ANTHROPIC_PREFIXES = ("anthropic.",)
_REGION_PREFIXES = ("us.", "eu.", "ap.", "apac.")


def uses_converse(model: str) -> bool:
    """Whether this Bedrock model id needs Converse rather than the Anthropic
    SDK.

    Only Claude keeps the Messages API, which supports its tool use natively.
    Everything else goes to Converse -- stated as "not Claude" rather than as
    a list of known vendors, so a model AWS adds tomorrow routes correctly
    instead of failing against a request shape it does not speak.
    """
    bare = model
    for prefix in _REGION_PREFIXES:
        if model.startswith(prefix):
            bare = model[len(prefix):]
            break
    return not bare.startswith(_ANTHROPIC_PREFIXES)


class BedrockConverseLLM:
    """Any Bedrock model, through AWS's uniform Converse API."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        region: str | None = None,
    ):
        import boto3

        self.provider = "bedrock"
        self.model = model
        self._region = region or "us-east-1"

        if api_key:
            # A Bedrock API key is a bearer token. boto3 has no parameter for
            # one, but botocore reads AWS_BEARER_TOKEN_BEDROCK from the
            # environment, which is how AWS's own documentation wires it up.
            import os

            os.environ.setdefault("AWS_BEARER_TOKEN_BEDROCK", api_key)
            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        else:
            self._client = boto3.client(
                "bedrock-runtime",
                region_name=self._region,
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
            )

    # ── message translation ──────────────────────────────────────────

    @staticmethod
    def _to_native(messages: list[dict]) -> list[dict]:
        """Our message shape to Converse's.

        Converse wraps every part in a typed block and names tool results
        differently from both other providers, which is the whole reason this
        translation exists rather than reusing one of theirs.
        """
        native: list[dict] = []
        for message in messages:
            role = message["role"]
            if role == "tool_results":
                native.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "toolResult": {
                                    "toolUseId": result["id"],
                                    "content": [{"text": str(result["content"])}],
                                }
                            }
                            for result in message["results"]
                        ],
                    }
                )
                continue

            content: list[dict] = []
            text = message.get("content")
            if text:
                content.append({"text": str(text)})
            for call in message.get("tool_calls", []) or []:
                content.append(
                    {
                        "toolUse": {
                            "toolUseId": call["id"],
                            "name": call["name"],
                            "input": call["args"],
                        }
                    }
                )
            if not content:
                # Converse rejects an empty content list outright.
                continue
            native.append({"role": role, "content": content})
        return native

    @staticmethod
    def _to_native_tools(tools: list[dict]) -> dict:
        return {
            "tools": [
                {
                    "toolSpec": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "inputSchema": {"json": tool["input_schema"]},
                    }
                }
                for tool in tools
            ]
        }

    async def _converse(self, **kwargs) -> dict:
        """One Converse call, off the event loop."""
        try:
            return await asyncio.to_thread(self._client.converse, **kwargs)
        except Exception as exc:
            # botocore raises ClientError for everything from a bad model id to
            # a throttle. The message carries which, and the caller surfaces it.
            raise LLMError(f"Bedrock Converse failed: {exc}") from exc

    # ── the interface every client implements ────────────────────────

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMReply:
        request = {
            "modelId": self.model,
            "messages": self._to_native(messages),
            "system": [{"text": system}],
            "inferenceConfig": {"maxTokens": _MAX_TOKENS},
        }
        if tools:
            request["toolConfig"] = self._to_native_tools(tools)

        response = await self._converse(**request)
        content = response.get("output", {}).get("message", {}).get("content", [])

        text_parts: list[str] = []
        calls: list[LLMToolCall] = []
        for block in content:
            if "text" in block:
                text_parts.append(block["text"])
            elif "toolUse" in block:
                use = block["toolUse"]
                calls.append(
                    LLMToolCall(
                        id=use.get("toolUseId", ""),
                        name=use.get("name", ""),
                        args=use.get("input") or {},
                    )
                )
        return LLMReply(text="\n".join(text_parts).strip(), tool_calls=calls)

    async def generate_json(self, system: str, prompt: str, schema: dict) -> dict:
        """Structured output.

        Converse has no universal JSON mode -- support varies by model and
        several have none -- so the schema is stated in the prompt and the
        reply parsed, tolerating code fences. _parse_json_text raises LLMError
        when a model returns prose instead, which is the honest failure: a
        strategy acting on a half-parsed decision would be worse.
        """
        instruction = (
            f"{system}\n\nReply with ONLY a JSON object matching this schema, "
            f"no prose and no code fence:\n{json.dumps(schema)}"
        )
        response = await self._converse(
            modelId=self.model,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=[{"text": instruction}],
            inferenceConfig={"maxTokens": _MAX_TOKENS},
        )
        content = response.get("output", {}).get("message", {}).get("content", [])
        text = "".join(block.get("text", "") for block in content)
        return _parse_json_text(text)
