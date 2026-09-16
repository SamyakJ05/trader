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
