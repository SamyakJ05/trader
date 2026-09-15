"""Provider-agnostic LLM access for the AI trading features.

Two implementations cover four providers:
- AnthropicLLM  -> anthropic (Claude API), bedrock (Claude on AWS Bedrock)
- OpenAILLM     -> openai, openrouter, or any OpenAI-compatible base_url

Resolution order: the user's ai_settings row (credentials Fernet-encrypted at
rest) -> ANTHROPIC_API_KEY env fallback -> None (AI features disabled).

Messages passed to chat() use a neutral shape, converted per provider:
  {"role": "user" | "assistant", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [LLMToolCall-dicts]}
  {"role": "tool_results", "results": [{"id", "content"}]}
Tools use the Anthropic shape ({name, description, input_schema}) and are
mapped to OpenAI function-calling internally.
"""

import json
import uuid as uuid_mod
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import decrypt_secret, encrypt_secret
from app.db.models import AISettings

PROVIDERS = ("anthropic", "openai", "openrouter", "bedrock")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "openai": "gpt-4o",
    "openrouter": "anthropic/claude-opus-5",
    "bedrock": "anthropic.claude-opus-5",
}
_MAX_TOKENS = 4096


class LLMError(Exception):
    """Provider call failed (auth, rate limit, transport, bad response)."""


@dataclass
class LLMToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMReply:
    text: str
    tool_calls: list[LLMToolCall] = field(default_factory=list)


class LLMClient(Protocol):
    provider: str
    model: str

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMReply: ...

    async def generate_json(self, system: str, prompt: str, schema: dict) -> dict: ...


def _parse_json_text(text: str) -> dict:
    """Parse a JSON object out of a model response, tolerating code fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        cleaned = cleaned.removeprefix("json").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise LLMError(f"Model did not return JSON: {text[:200]}")
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"Model returned invalid JSON: {e}") from e


JSON_ONLY_INSTRUCTION = (
    "Respond ONLY with a single JSON object matching this schema — no prose, "
    "no code fences:\n{schema}"
)


class AnthropicLLM:
    """Claude via the Anthropic API or AWS Bedrock (same SDK, same shapes)."""

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        provider: str = "anthropic",
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        region: str | None = None,
    ):
        import anthropic

        self.provider = provider
        self.model = model
        self._anthropic = anthropic
        if provider == "bedrock":
            self._client = anthropic.AsyncAnthropicBedrock(
                aws_access_key=aws_access_key_id,
                aws_secret_key=aws_secret_access_key,
                aws_region=region or "us-east-1",
            )
        else:
            self._client = anthropic.AsyncAnthropic(api_key=api_key)

    @staticmethod
    def _to_native(messages: list[dict]) -> list[dict]:
        native: list[dict] = []
        for m in messages:
            if m["role"] == "tool_results":
                native.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": r["id"],
                                "content": r["content"],
                            }
                            for r in m["results"]
                        ],
                    }
                )
            elif m["role"] == "assistant" and m.get("tool_calls"):
                content: list[dict] = []
                if m.get("content"):
                    content.append({"type": "text", "text": m["content"]})
                content.extend(
                    {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}
                    for c in m["tool_calls"]
                )
                native.append({"role": "assistant", "content": content})
            else:
                native.append({"role": m["role"], "content": m["content"]})
        return native

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMReply:
        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=_MAX_TOKENS,
                system=system,
                messages=self._to_native(messages),
                tools=tools,
            )
        except self._anthropic.APIError as e:
            raise LLMError(str(e)) from e
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [
            LLMToolCall(id=b.id, name=b.name, args=dict(b.input))
            for b in resp.content
            if b.type == "tool_use"
        ]
        return LLMReply(text=text, tool_calls=calls)

    async def generate_json(self, system: str, prompt: str, schema: dict) -> dict:
        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=_MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except self._anthropic.BadRequestError:
            # Model without structured-output support — fall back to prompting.
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=_MAX_TOKENS,
                system=system,
                messages=[
                    {
                        "role": "user",
                        "content": f"{prompt}\n\n"
                        + JSON_ONLY_INSTRUCTION.format(schema=json.dumps(schema)),
                    }
                ],
            )
        except self._anthropic.APIError as e:
            raise LLMError(str(e)) from e
        text = "".join(b.text for b in resp.content if b.type == "text")
        return _parse_json_text(text)


def tools_to_openai(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


class OpenAILLM:
    """OpenAI, OpenRouter, or any OpenAI-compatible endpoint."""

    def __init__(self, model: str, api_key: str, base_url: str | None = None, provider: str = "openai"):
        import openai

        self.provider = provider
        self.model = model
        self._openai = openai
        self._client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)

    @staticmethod
    def _to_native(system: str, messages: list[dict]) -> list[dict]:
        native: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "tool_results":
                native.extend(
                    {"role": "tool", "tool_call_id": r["id"], "content": r["content"]}
                    for r in m["results"]
                )
            elif m["role"] == "assistant" and m.get("tool_calls"):
                native.append(
                    {
                        "role": "assistant",
                        "content": m.get("content") or None,
                        "tool_calls": [
                            {
                                "id": c["id"],
                                "type": "function",
                                "function": {
                                    "name": c["name"],
                                    "arguments": json.dumps(c["args"]),
                                },
                            }
                            for c in m["tool_calls"]
                        ],
                    }
                )
            else:
                native.append({"role": m["role"], "content": m["content"]})
        return native

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> LLMReply:
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                max_tokens=_MAX_TOKENS,
                messages=self._to_native(system, messages),
                tools=tools_to_openai(tools) if tools else self._openai.NOT_GIVEN,
            )
        except self._openai.OpenAIError as e:
            raise LLMError(str(e)) from e
        msg = resp.choices[0].message
        calls = []
        for c in msg.tool_calls or []:
            try:
                args = json.loads(c.function.arguments)
            except json.JSONDecodeError:
                args = {}
            calls.append(LLMToolCall(id=c.id, name=c.function.name, args=args))
        return LLMReply(text=msg.content or "", tool_calls=calls)

    async def generate_json(self, system: str, prompt: str, schema: dict) -> dict:
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                max_tokens=_MAX_TOKENS,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "output", "schema": schema},
                },
            )
        except self._openai.BadRequestError:
            # Endpoint without json_schema support (some OpenRouter routes).
            try:
                resp = await self._client.chat.completions.create(
                    model=self.model,
                    max_tokens=_MAX_TOKENS,
                    messages=[
                        {"role": "system", "content": system},
                        {
                            "role": "user",
                            "content": f"{prompt}\n\n"
                            + JSON_ONLY_INSTRUCTION.format(schema=json.dumps(schema)),
                        },
                    ],
                )
            except self._openai.OpenAIError as e:
                raise LLMError(str(e)) from e
        except self._openai.OpenAIError as e:
            raise LLMError(str(e)) from e
        return _parse_json_text(resp.choices[0].message.content or "")


# ── settings resolution ──────────────────────────────────────────────


def build_credentials_blob(
    provider: str,
    api_key: str | None,
    aws_access_key_id: str | None = None,
    aws_secret_access_key: str | None = None,
    region: str | None = None,
) -> str:
    if provider == "bedrock":
        creds = {
            "aws_access_key_id": aws_access_key_id,
            "aws_secret_access_key": aws_secret_access_key,
            "region": region,
        }
    else:
        creds = {"api_key": api_key}
    return encrypt_secret(json.dumps(creds))


def _client_from_row(row: AISettings) -> LLMClient | None:
    if not row.credentials_enc:
        return None
    creds = json.loads(decrypt_secret(row.credentials_enc))
    if row.provider == "bedrock":
        return AnthropicLLM(
            model=row.model,
            provider="bedrock",
            aws_access_key_id=creds.get("aws_access_key_id"),
            aws_secret_access_key=creds.get("aws_secret_access_key"),
            region=creds.get("region"),
        )
    if row.provider == "anthropic":
        return AnthropicLLM(model=row.model, api_key=creds.get("api_key"))
    base_url = row.base_url or (OPENROUTER_BASE_URL if row.provider == "openrouter" else None)
    return OpenAILLM(
        model=row.model,
        api_key=creds.get("api_key") or "",
        base_url=base_url,
        provider=row.provider,
    )


async def get_ai_settings(db: AsyncSession, user_id: uuid_mod.UUID) -> AISettings | None:
    result = await db.execute(select(AISettings).where(AISettings.user_id == user_id))
    return result.scalar_one_or_none()


async def resolve_llm(db: AsyncSession, user_id: uuid_mod.UUID) -> LLMClient | None:
    """User's configured provider, falling back to the env ANTHROPIC_API_KEY."""
    row = await get_ai_settings(db, user_id)
    if row is not None:
        client = _client_from_row(row)
        if client is not None:
            return client
    settings = get_settings()
    if settings.anthropic_api_key:
        return AnthropicLLM(model=settings.ai_model, api_key=settings.anthropic_api_key)
    return None
