"""The Bedrock model id is the one setting a user cannot derive.

Both ways of getting it wrong fail with messages that name no fix, so these
pin the two things that turn a dead end into a next step: a hint that tells
AccessDenied and ValidationException apart, and a listing route the UI can
call at the path the UI actually calls.
"""

from app.api.routes.ai import _provider_hint

ACCESS_DENIED = (
    "Bedrock Converse failed: An error occurred (AccessDeniedException) when "
    "calling the Converse operation: openai.gpt-6-astra is not available for "
    "this account. You can explore other available models on Amazon Bedrock."
)
INVALID_ID = (
    "Bedrock Converse failed: An error occurred (ValidationException) when "
    "calling the Converse operation: The provided model identifier is invalid."
)


def test_invalid_identifier_is_not_reported_as_an_access_problem():
    """The two AWS errors need opposite actions, so they must not share a hint.

    "Not available for this account" means request access; "identifier is
    invalid" means AWS never recognised the string, and sending that user to
    the access-request page wastes a day waiting for a grant that changes
    nothing.
    """
    hint = _provider_hint("bedrock", INVALID_ID)
    assert hint is not None
    assert "not an access problem" in hint
    assert "List models" in hint


def test_malformed_id_names_the_missing_pieces():
    hint = _provider_hint("bedrock", ACCESS_DENIED)
    assert hint is not None
    # Both defects in the id the user actually entered.
    assert "region prefix" in hint
    assert "-v1:0" in hint


def test_wellformed_id_is_not_called_malformed():
    """A correctly shaped id that is genuinely not granted must not be
    blamed on a typo -- that would send the user to fix nothing."""
    denied = ACCESS_DENIED.replace(
        "openai.gpt-6-astra", "us.openai.gpt-6-astra-v1:0"
    )
    hint = _provider_hint("bedrock", denied)
    assert hint is not None
    assert "not shaped like" not in hint


def test_listing_route_is_mounted_where_the_ui_calls_it():
    """A mismatch here is silent -- the button would simply always fail.

    Asserted through the OpenAPI schema rather than app.routes: this FastAPI
    version keeps an included router as a single lazy _IncludedRouter node, so
    walking app.routes finds no API paths at all and a path assertion against
    it passes or fails for the wrong reason. The schema is the resolved view,
    and is what a client sees.

    The literal is the full served path. AISettingsCard calls
    "/ai/settings/bedrock-models" against an API_BASE that already ends in
    /api/v1, so the two must agree exactly here.
    """
    from app.main import app

    assert "/api/v1/ai/settings/bedrock-models" in app.openapi()["paths"]
