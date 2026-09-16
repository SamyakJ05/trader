"""Claude on AWS Bedrock.

Bedrock is a different authentication model from every other provider here:
three AWS values rather than one API key, and requests signed with SigV4
rather than sent with a bearer token. That signing is done by botocore, which
the anthropic SDK only pulls in under its [bedrock] extra.

Without the extra the client CONSTRUCTS fine and raises ModuleNotFoundError
on the first real call -- in the worker, where nobody is watching, so AI
strategies would stop producing signals with no visible error. That is what
these pin.
"""

import importlib.util

from app.api.routes.ai import _provider_hint
from app.services.ai.llm import DEFAULT_MODELS, PROVIDERS


def test_the_signing_dependency_is_installed():
    """AsyncAnthropicBedrock signs with SigV4 via botocore. Verified by
    importing rather than by reading pyproject, because the question is what
    the running image has."""
    assert importlib.util.find_spec("botocore") is not None


def test_the_image_declares_the_bedrock_extra():
    """anthropic alone does not bring boto3 -- it is not in its requires. The
    extra is what does, and a plain `anthropic>=0.40` in the image is the
    failure this catches."""
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert "anthropic[bedrock]" in pyproject.read_text()


def test_bedrock_is_a_registered_provider():
    assert "bedrock" in PROVIDERS
    assert DEFAULT_MODELS["bedrock"]


# ── the hints, which are what an operator actually sees ──────────────


def test_rejected_credentials_explain_which_key_is_wanted():
    """The likeliest mistake is pasting a Claude API key. AWS answers that
    with "the security token included in the request is invalid", which does
    not say what kind of token it wanted."""
    hint = _provider_hint(
        "bedrock", "Error code: 403 - The security token included in the request is invalid."
    )
    assert hint and "IAM access key" in hint
    assert "not a Claude API key" in hint


def test_a_rejected_model_id_explains_inference_profiles():
    """AWS reports this as a ValidationException naming the id, which reads
    like the model does not exist rather than like it is not addressable that
    way in this region."""
    hint = _provider_hint("bedrock", "ValidationException: model anthropic.claude-opus-5")
    assert hint and "inference profile" in hint.replace("profiles", "profile")


def test_a_permissions_failure_names_the_action():
    hint = _provider_hint("bedrock", "AccessDeniedException: not authorized")
    assert hint and "bedrock:InvokeModel" in hint


def test_other_providers_get_no_bedrock_hint():
    """A hint about IAM keys on an OpenAI failure would send someone the
    wrong way entirely."""
    assert _provider_hint("anthropic", "401 invalid x-api-key") is None
    assert _provider_hint("openai", "429 rate limit") is None


# ── the bearer-token credential ──────────────────────────────────────


def test_a_bedrock_api_key_is_a_complete_credential():
    """An Amazon Bedrock API key is a bearer token, and the SDK's api_key
    parameter is exactly that for the Bedrock client.

    I originally reported this as unsupported after reading the constructor's
    parameter names, which list aws_access_key/aws_secret_key/aws_region. The
    api_key parameter is there too and falls back to AWS_BEARER_TOKEN_BEDROCK
    -- so the platform was refusing a credential it could always have used.
    """
    import inspect

    import anthropic

    source = inspect.getsource(anthropic.AsyncAnthropicBedrock.__init__)
    assert "AWS_BEARER_TOKEN_BEDROCK" in source


def test_the_client_prefers_a_bearer_token_over_iam(monkeypatch):
    """An operator who generated a Bedrock API key chose it deliberately.
    Falling through to SigV4 with half-configured IAM values would fail in a
    way that points at the wrong credential."""
    from app.services.ai.llm import AnthropicLLM

    captured = {}

    class FakeBedrock:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropicBedrock", FakeBedrock)

    AnthropicLLM(
        model="anthropic.claude-opus-5",
        provider="bedrock",
        api_key="ABSKtoken",
        aws_access_key_id="AKIA-ignored",
        aws_secret_access_key="ignored",
        region="ap-south-1",
    )
    assert captured.get("api_key") == "ABSKtoken"
    assert "aws_access_key" not in captured
    assert captured.get("aws_region") == "ap-south-1"


def test_iam_credentials_still_work_when_no_token_is_given(monkeypatch):
    from app.services.ai.llm import AnthropicLLM

    captured = {}

    class FakeBedrock:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropicBedrock", FakeBedrock)

    AnthropicLLM(
        model="anthropic.claude-opus-5",
        provider="bedrock",
        aws_access_key_id="AKIAEXAMPLE",
        aws_secret_access_key="secret",
        region="us-east-1",
    )
    assert captured.get("aws_access_key") == "AKIAEXAMPLE"
    assert captured.get("api_key") is None or "api_key" not in captured


def test_a_bearer_token_survives_a_round_trip_through_storage():
    """Credentials are Fernet-encrypted as a JSON blob. A token dropped on the
    way in or out would fail later as an auth error, pointing at AWS rather
    than at us."""
    import json

    from app.core.security import decrypt_secret
    from app.services.ai.llm import build_credentials_blob

    blob = build_credentials_blob(
        "bedrock", "ABSKtoken", None, None, "ap-south-1"
    )
    creds = json.loads(decrypt_secret(blob))
    assert creds["api_key"] == "ABSKtoken"
    assert creds["region"] == "ap-south-1"


def test_an_unavailable_model_is_not_reported_as_a_credential_problem():
    """Bedrock answers BOTH a bad credential and an unavailable model with
    403. Keying the hint on the status code told an operator whose key was
    working perfectly to go and check their key -- the exact wrong direction,
    and the kind of message that costs an hour.

    This is the real error text from a live account.
    """
    error = (
        "Error code: 403 - {'message': 'anthropic.claude-opus-5 is not "
        "available for this account. You can explore other available models "
        "on Amazon Bedrock.'}"
    )
    hint = _provider_hint("bedrock", error)
    assert hint
    assert "credentials worked" in hint
    assert "Model catalog" in hint
    # Must NOT send them back to the credential.
    assert "IAM access key" not in hint


def test_a_genuine_credential_failure_still_says_so():
    """The fix must not swing the other way: a real auth failure should still
    point at the credential."""
    hint = _provider_hint(
        "bedrock", "403 The security token included in the request is invalid."
    )
    assert hint and "IAM access key" in hint
