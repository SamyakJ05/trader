"""A saved AI configuration is authoritative.

Found in production. An account had configured Bedrock with zai.glm-5, which
works -- the model was verified replying directly. The AI tab still failed,
with Bedrock returning 403 for anthropic.claude-opus-5: a model that account
cannot call and never chose.

resolve_llm was falling through to the environment whenever the saved row
could not produce a client, so a configuration problem was answered by
silently running a different model on different credentials. The 403 named a
model the person had never heard of, which is the worst possible clue.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.services.ai import llm as llm_module
from app.services.ai.llm import AIConfigurationError, resolve_llm


class FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class FakeDb:
    def __init__(self, row):
        self._row = row

    async def execute(self, *a, **kw):
        return FakeResult(self._row)


def row(provider="bedrock", model="zai.glm-5", credentials_enc="enc:x"):
    return SimpleNamespace(
        user_id=uuid.uuid4(),
        provider=provider,
        model=model,
        base_url=None,
        credentials_enc=credentials_enc,
    )


async def test_a_working_configuration_is_used(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(llm_module, "_client_from_row", lambda r: sentinel)
    assert await resolve_llm(FakeDb(row()), uuid.uuid4()) is sentinel


async def test_a_broken_configuration_raises_instead_of_falling_back(monkeypatch):
    """The regression. Falling through here sent requests to the environment's
    model -- anthropic.claude-opus-5 -- on an account that had configured
    zai.glm-5, and the 403 named a model the user never chose."""
    monkeypatch.setattr(llm_module, "_client_from_row", lambda r: None)
    monkeypatch.setattr(
        llm_module,
        "get_settings",
        lambda: SimpleNamespace(anthropic_api_key="env-key", ai_model="claude-opus-5"),
    )

    with pytest.raises(AIConfigurationError) as exc:
        await resolve_llm(FakeDb(row()), uuid.uuid4())
    # The message has to name the provider they configured and where to fix it.
    assert "bedrock" in str(exc.value)
    assert "AI Trading settings" in str(exc.value)


async def test_the_environment_is_never_substituted_for_a_saved_choice(monkeypatch):
    """Even with a perfectly good environment provider available, a user who
    has configured their own must not silently get the environment's."""
    monkeypatch.setattr(llm_module, "_client_from_row", lambda r: None)
    monkeypatch.setattr(
        llm_module,
        "get_settings",
        lambda: SimpleNamespace(anthropic_api_key="env-key", ai_model="claude-opus-5"),
    )
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "token")

    with pytest.raises(AIConfigurationError):
        await resolve_llm(FakeDb(row()), uuid.uuid4())


async def test_a_user_with_no_configuration_still_gets_the_environment(monkeypatch):
    """The fallback remains right for someone who has configured nothing --
    that is not substituting for a choice, it is the only choice available."""
    made = {}

    class FakeAnthropic:
        def __init__(self, model, api_key):
            made["model"] = model

    monkeypatch.setattr(llm_module, "AnthropicLLM", FakeAnthropic)
    monkeypatch.setattr(
        llm_module,
        "get_settings",
        lambda: SimpleNamespace(anthropic_api_key="env-key", ai_model="claude-opus-5"),
    )

    client = await resolve_llm(FakeDb(None), uuid.uuid4())
    assert isinstance(client, FakeAnthropic)
    assert made["model"] == "claude-opus-5"


async def test_no_configuration_anywhere_is_none_not_an_error(monkeypatch):
    """None means 'nothing is set up', which the callers report as a 503 with
    setup instructions. It must not be conflated with a broken setup."""
    monkeypatch.setattr(
        llm_module,
        "get_settings",
        lambda: SimpleNamespace(anthropic_api_key=None, ai_model="claude-opus-5"),
    )
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    assert await resolve_llm(FakeDb(None), uuid.uuid4()) is None


async def test_the_research_pass_reports_a_bad_provider_distinctly(monkeypatch):
    """The daily job shares this path. A broken configuration should be
    logged as such rather than as 'no provider', which reads as 'not set up
    yet' and hides a thing the person could fix."""
    from app.services.ai import research

    async def explode(db, user_id):
        raise AIConfigurationError("bedrock credentials incomplete")

    async def some_candidates(db, account):
        return ["RELIND"]

    monkeypatch.setattr(research, "resolve_llm", explode)
    monkeypatch.setattr(research, "candidates", some_candidates)

    class Db:
        async def execute(self, *a, **kw):
            class R:
                def scalars(self):
                    return []

            return R()

    result = await research.run_daily_research(
        Db(), None, SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(),
                                    broker="icici_breeze", environment="live")
    )
    assert result["status"] == "bad_provider"
    assert result["proposals"] == []
