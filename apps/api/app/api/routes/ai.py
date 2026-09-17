"""AI trading surface: provider settings, analyst chat, trade proposals,
and the strategy generator. Secrets are encrypted at rest and never returned.
Approval is the only path from a proposal to an order, and it goes through
the standard order pipeline."""

import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.config import get_settings
from app.core.deps import DbSession, VerifiedUser
from app.core.redis import get_redis
from app.db.models import AIProposal, AISettings
from app.domain.enums import (
    AIProposalStatus,
    AuditEventType,
)
from app.services import audit
from app.services import brokers as broker_service
from app.services.ai import analyst, generator
from app.services.ai import execute as ai_execute
from app.services.ai.llm import (
    DEFAULT_MODELS,
    OPENROUTER_BASE_URL,
    PROVIDERS,
    AIConfigurationError,
    LLMError,
    build_credentials_blob,
    get_ai_settings,
    resolve_llm,
)

router = APIRouter(prefix="/ai", tags=["ai"])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── status + settings ────────────────────────────────────────────────


@router.get("/status")
async def ai_status(user: VerifiedUser, db: DbSession):
    row = await get_ai_settings(db, user.id)
    if row is not None and row.credentials_enc:
        return {"configured": True, "provider": row.provider, "model": row.model}
    settings = get_settings()
    if settings.anthropic_api_key:
        return {"configured": True, "provider": "anthropic", "model": settings.ai_model}
    return {"configured": False, "provider": None, "model": None}


class AISettingsBody(BaseModel):
    provider: str
    model: str = Field(min_length=1, max_length=128)
    base_url: str | None = None
    api_key: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    region: str | None = None


@router.get("/settings")
async def read_settings(user: VerifiedUser, db: DbSession):
    row = await get_ai_settings(db, user.id)
    env_fallback = bool(get_settings().anthropic_api_key)
    if row is None:
        return {
            "provider": "anthropic" if env_fallback else None,
            "model": get_settings().ai_model if env_fallback else None,
            "base_url": None,
            "configured": env_fallback,
            "source": "env" if env_fallback else None,
            "providers": list(PROVIDERS),
            "default_models": DEFAULT_MODELS,
        }
    return {
        "provider": row.provider,
        "model": row.model,
        "base_url": row.base_url,
        "configured": bool(row.credentials_enc),
        "source": "settings",
        "providers": list(PROVIDERS),
        "default_models": DEFAULT_MODELS,
    }


@router.put("/settings")
async def write_settings(body: AISettingsBody, user: VerifiedUser, db: DbSession):
    if body.provider not in PROVIDERS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown provider {body.provider!r}; supported: {list(PROVIDERS)}",
        )
    if body.provider == "bedrock":
        # Bedrock accepts either credential form: an Amazon Bedrock API key
        # (a bearer token from the Bedrock console) or an IAM access key pair
        # signed with SigV4. Requiring the IAM pair rejected the simpler one,
        # which is what the console hands you by default.
        if not body.region:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Bedrock needs a region (for example us-east-1)",
            )
        has_bearer = bool(body.api_key)
        has_iam = bool(body.aws_access_key_id and body.aws_secret_access_key)
        if not (has_bearer or has_iam):
            existing = await get_ai_settings(db, user.id)
            # Allow a model or region change without re-entering credentials.
            if existing is None or existing.provider != "bedrock" or not existing.credentials_enc:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "Bedrock needs either an Amazon Bedrock API key, or an IAM "
                    "access key id and secret access key.",
                )
    elif not body.api_key:
        row = await get_ai_settings(db, user.id)
        # Allow model/base_url updates without re-entering the key.
        if row is None or row.provider != body.provider or not row.credentials_enc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "api_key is required")

    row = await get_ai_settings(db, user.id)
    if row is None:
        row = AISettings(user_id=user.id, provider=body.provider, model=body.model)
        db.add(row)

    row.provider = body.provider
    row.model = body.model
    row.base_url = body.base_url or (
        OPENROUTER_BASE_URL if body.provider == "openrouter" else None
    )
    # Only rewrite credentials when some were supplied. For bedrock this used
    # to rewrite unconditionally, so changing just the model or region blanked
    # a stored key and the next call failed on credentials the operator
    # believed were still there.
    supplied = bool(
        body.api_key or (body.aws_access_key_id and body.aws_secret_access_key)
    )
    if supplied:
        row.credentials_enc = build_credentials_blob(
            body.provider,
            body.api_key,
            body.aws_access_key_id,
            body.aws_secret_access_key,
            body.region,
        )
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=user.id,
        entity_type="ai_settings",
        payload={"action": "update", "provider": body.provider, "model": body.model},
    )
    await db.commit()
    return {"provider": row.provider, "model": row.model, "configured": True}


@router.post("/settings/test")
async def test_settings(user: VerifiedUser, db: DbSession):
    try:
        llm = await resolve_llm(db, user.id)
    except AIConfigurationError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if llm is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AI is not configured")
    try:
        out = await llm.generate_json(
            "You are a connectivity check.",
            "Reply with JSON: {\"ok\": true}",
            {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
        )
        return {"ok": bool(out.get("ok")), "provider": llm.provider, "model": llm.model}
    except LLMError as e:
        return {
            "ok": False,
            "error": str(e)[:300],
            "hint": _provider_hint(llm.provider, str(e)),
        }


@router.get("/settings/bedrock-models")
async def bedrock_models(user: VerifiedUser, db: DbSession):
    """The model ids THIS account can call, asked of AWS.

    Exists because every other way of getting an id is transcription from a
    console page, and the two ways that goes wrong -- copying the card title,
    or copying the bare id where only the cross-region profile works -- fail
    with messages that name no fix ("not available for this account", "the
    provided model identifier is invalid").
    """
    try:
        llm = await resolve_llm(db, user.id)
    except AIConfigurationError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if llm is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AI is not configured")
    if llm.provider != "bedrock":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Model listing is Bedrock-only; other providers publish a fixed list.",
        )
    lister = getattr(llm, "list_models", None)
    if lister is None:
        # A Claude-on-Bedrock client goes through the Anthropic SDK, which has
        # no listing call. Route through Converse purely to ask.
        from app.services.ai.bedrock_converse import BedrockConverseLLM

        lister = BedrockConverseLLM(
            model=llm.model,
            api_key=getattr(llm, "_api_key", None),
            region=getattr(llm, "_region", None),
        ).list_models
    try:
        return {"models": await lister()}
    except LLMError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)[:300]) from e


# A real Bedrock model id is vendor.name-vNUMBER:NUMBER, optionally prefixed
# with a region for a cross-region inference profile. The console's card
# TITLE ("GPT-6 Astra") is not an id, and typing it produces exactly the
# AccessDenied this catches.
_BEDROCK_ID = re.compile(
    r"^(?:(?:us|eu|apac|ap)\.)?[a-z0-9-]+\.[\w.-]+-v\d+:\d+$"
)


def _malformed_bedrock_id(error: str) -> str | None:
    """Whether the id AWS rejected is the wrong SHAPE, and what is missing.

    Worth separating from "not granted": the two look identical in AWS's
    message but need opposite actions -- request access, versus fix a typo.
    """
    match = re.search(r"([\w.:-]+) is not available for this account", error)
    if not match:
        return None
    model = match.group(1)
    if _BEDROCK_ID.match(model):
        return None  # Well-formed; genuinely not granted.

    missing = []
    if not re.match(r"^(?:us|eu|apac|ap)\.", model):
        missing.append(
            "a region prefix (us. / eu. / apac.) if the console shows it as "
            "'Cross-region inference'"
        )
    if not re.search(r"-v\d+:\d+$", model):
        missing.append("the version suffix (for example -v1:0)")
    if not missing:
        return None
    return (
        f"The id you entered, {model!r}, is not shaped like a Bedrock model "
        f"id — it is missing {' and '.join(missing)}."
    )


def _provider_hint(provider: str, error: str) -> str | None:
    """A next step for the failures that are not obvious from the message.

    Bedrock's are the worst of these: AWS reports a rejected model id as a
    ValidationException naming the id, which reads like the model does not
    exist rather than like it is not enabled or not addressable this way in
    this region.
    """
    lowered = error.lower()
    if provider != "bedrock":
        return None
    # Matched on the message, not the status code. Bedrock answers BOTH a bad
    # credential and an unavailable model with 403, so keying on the code told
    # an operator whose key was fine to go and check their key.
    if "not available for this account" in lowered or "explore other available" in lowered:
        base = (
            "The credentials worked; AWS refused the MODEL. Open the Bedrock "
            "console in the SAME region, find the model in Model catalog, "
            "confirm it says 'Access granted', and copy its Model ID field "
            "verbatim — not the title on the card."
        )
        malformed = _malformed_bedrock_id(error)
        return f"{base} {malformed}" if malformed else base
    if "security token" in lowered or "unrecognizedclient" in lowered:
        return (
            "AWS rejected the credentials. Bedrock needs an IAM access key "
            "with bedrock:InvokeModel, not a Claude API key — check the key, "
            "the secret and the region all belong to the same account."
        )
    if "model identifier is invalid" in lowered:
        # Distinct from "not available for this account": AWS did not even
        # recognise the string as an id, so this is never an access problem.
        return (
            "AWS did not recognise that string as a model id at all — this is "
            "not an access problem, so requesting access will not fix it. Use "
            "'List models' to pull the ids your account can actually call."
        )
    if "validation" in lowered or "model" in lowered:
        return (
            "AWS rejected the model id. Many models are addressable only as a "
            "region-prefixed inference profile (us.… / eu.… / apac.…) rather "
            "than the bare vendor id, and the model must be enabled for your "
            "account in the region you selected. Use 'List models' to pull "
            "the exact ids your account can call."
        )
    if "accessdenied" in lowered:
        return (
            "The credentials are valid but lack permission. The IAM policy "
            "needs bedrock:InvokeModel for this model in this region."
        )
    return None


async def _require_llm(db, user_id):
    try:
        llm = await resolve_llm(db, user_id)
    except AIConfigurationError as exc:
        # The saved configuration is broken, which is a different problem from
        # having none -- and the only one the person can fix from the UI.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if llm is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AI is not configured — add a provider in AI Trading settings "
            "or set ANTHROPIC_API_KEY in the backend env",
        )
    return llm


# ── analyst chat ─────────────────────────────────────────────────────


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=8000)


class ChatBody(BaseModel):
    broker_account_id: uuid.UUID
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)


@router.post("/analyst/chat")
async def analyst_chat(body: ChatBody, user: VerifiedUser, db: DbSession):
    llm = await _require_llm(db, user.id)
    account = await broker_service.get_account(db, user.id, body.broker_account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Broker account not found")
    if account.environment != "paper":
        raise HTTPException(status.HTTP_409_CONFLICT, "AI analyst is paper-only")

    try:
        result = await analyst.chat(
            db,
            get_redis(),
            llm,
            user.id,
            account,
            [m.model_dump() for m in body.messages],
        )
    except LLMError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"AI provider error: {e}") from e

    proposals = []
    for pid in result["proposal_ids"]:
        p = await db.get(AIProposal, uuid.UUID(pid))
        if p is not None:
            proposals.append(_proposal_out(p))
    return {"reply": result["reply"], "proposals": proposals}


# ── proposals ────────────────────────────────────────────────────────


def _proposal_out(p: AIProposal) -> dict:
    return {
        "id": str(p.id),
        "broker_account_id": str(p.broker_account_id),
        "symbol": p.symbol,
        "exchange": p.exchange,
        "side": p.side,
        "order_type": p.order_type,
        "product": p.product,
        "quantity": p.quantity,
        "limit_price": str(p.limit_price) if p.limit_price is not None else None,
        # The contract. A user approving an option trade must see which
        # contract it is -- "65 x NIFTY" alone does not say, and the same
        # underlying has thousands of them.
        "expiry": p.expiry.isoformat() if p.expiry else None,
        "strike": str(p.strike) if p.strike is not None else None,
        "option_right": p.option_right,
        "rationale": p.rationale,
        "status": p.status,
        "order_id": str(p.order_id) if p.order_id else None,
        "created_at": p.created_at.isoformat(),
        "decided_at": p.decided_at.isoformat() if p.decided_at else None,
    }


@router.get("/proposals")
async def list_proposals(user: VerifiedUser, db: DbSession, status_filter: str | None = None):
    query = select(AIProposal).where(AIProposal.user_id == user.id)
    if status_filter:
        query = query.where(AIProposal.status == status_filter)
    query = query.order_by(AIProposal.created_at.desc()).limit(50)
    result = await db.execute(query)
    return [_proposal_out(p) for p in result.scalars()]


async def _owned_proposal(db, user, proposal_id: uuid.UUID) -> AIProposal:
    p = await db.get(AIProposal, proposal_id)
    if p is None or p.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Proposal not found")
    return p


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: uuid.UUID, user: VerifiedUser, db: DbSession):
    p = await _owned_proposal(db, user, proposal_id)
    if p.status != AIProposalStatus.PROPOSED.value:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Proposal already {p.status}")
    # Re-verify ownership rather than trusting the stored FK: this is an
    # order-placement path, and place_order refuses a mismatch anyway.
    account = await broker_service.get_account(db, user.id, p.broker_account_id)
    if account is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Broker account no longer exists or is not yours"
        )

    # Routed through the shared proposal executor, which is the same code the
    # auto-execute path uses. A proposal carrying MARKET/MIS -- which the tool
    # schema used to allow on any broker -- would otherwise reach a Breeze
    # account as an order it refuses on both counts, after the user had
    # already approved a real trade.
    try:
        order = await ai_execute.place_from_proposal(db, p, account, auto_executed=False)
    except ai_execute.ProposalNotExecutable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    p.status = AIProposalStatus.APPROVED.value
    p.order_id = order.id
    p.decided_at = utcnow()
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=user.id,
        entity_type="ai_proposal",
        entity_id=p.id,
        payload={"action": "approve", "order_id": str(order.id), "order_status": order.status},
    )
    await db.commit()
    return {"proposal": _proposal_out(p), "order_status": order.status}


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: uuid.UUID, user: VerifiedUser, db: DbSession):
    p = await _owned_proposal(db, user, proposal_id)
    if p.status != AIProposalStatus.PROPOSED.value:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Proposal already {p.status}")
    p.status = AIProposalStatus.REJECTED.value
    p.decided_at = utcnow()
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=user.id,
        entity_type="ai_proposal",
        entity_id=p.id,
        payload={"action": "reject"},
    )
    await db.commit()
    return _proposal_out(p)


# ── strategy generator ───────────────────────────────────────────────


class GenerateBody(BaseModel):
    prompt: str = Field(min_length=8, max_length=2000)
    # Optional so an existing client keeps working, but without it the model
    # is told the generic NSE/MIS rules -- which are wrong for Breeze on both
    # counts, and every strategy generated for one would emit orders the
    # adapter refuses.
    broker_account_id: uuid.UUID | None = None


@router.post("/strategies/generate")
async def generate_strategy(body: GenerateBody, user: VerifiedUser, db: DbSession):
    llm = await _require_llm(db, user.id)
    broker = environment = None
    if body.broker_account_id is not None:
        account = await broker_service.get_account(db, user.id, body.broker_account_id)
        if account is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Broker account not found")
        broker, environment = account.broker, account.environment
    try:
        return await generator.generate(
            llm, body.prompt, broker=broker, environment=environment
        )
    except LLMError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"AI provider error: {e}") from e
