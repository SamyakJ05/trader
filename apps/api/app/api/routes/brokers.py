import uuid
from urllib.parse import quote, urlencode
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.adapters.base import BrokerError
from app.adapters.registry import get_adapter
from app.adapters.zerodha.adapter import ZerodhaAdapter
from app.core.config import get_settings
from app.core.deps import DbSession, VerifiedUser
from app.core.redis import get_redis
from app.db.models import BrokerAccount, CashLedger
from app.domain.capabilities import CAPABILITY_MATRIX
from app.domain.enums import AuditEventType, Broker, Environment
from app.services import audit
from app.services import brokers as broker_service
from app.services import oauth_state

router = APIRouter(prefix="/brokers", tags=["brokers"])


class CreateAccountRequest(BaseModel):
    broker: Broker
    label: str = Field(min_length=1, max_length=64)
    environment: Environment = Environment.PAPER
    credential_ref: str | None = Field(
        default=None,
        description="Env-var prefix for credentials, e.g. ZERODHA_MAIN",
    )
    broker_client_id: str | None = None


class AccountOut(BaseModel):
    id: str
    broker: str
    label: str
    environment: str
    status: str
    status_message: str | None
    live_enabled: bool
    last_sync_at: datetime | None
    read_verified_at: datetime | None
    credential_ref: str | None
    broker_client_id: str | None
    adapter_status: str
    # Masked metadata only — secret material never leaves the backend.
    credentials: dict


def _account_out(a: BrokerAccount) -> AccountOut:
    return AccountOut(
        id=str(a.id),
        broker=a.broker,
        label=a.label,
        environment=a.environment,
        status=a.status,
        status_message=a.status_message,
        live_enabled=a.live_enabled,
        last_sync_at=a.last_sync_at,
        read_verified_at=a.read_verified_at,
        credential_ref=a.credential_ref,
        broker_client_id=a.broker_client_id,
        adapter_status=CAPABILITY_MATRIX[Broker(a.broker)].adapter_status.value,
        credentials=broker_service.credential_status(a),
    )


@router.get("/capabilities")
async def capabilities():
    return {broker.value: caps.model_dump() for broker, caps in CAPABILITY_MATRIX.items()}


@router.get("/accounts", response_model=list[AccountOut])
async def list_accounts(user: VerifiedUser, db: DbSession):
    result = await db.execute(
        select(BrokerAccount)
        .where(BrokerAccount.user_id == user.id)
        .order_by(BrokerAccount.created_at)
    )
    return [_account_out(a) for a in result.scalars()]


@router.post("/accounts", response_model=AccountOut, status_code=201)
async def create_account(body: CreateAccountRequest, user: VerifiedUser, db: DbSession):
    if body.environment == Environment.LIVE and body.broker == Broker.PAPER:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Paper broker cannot be live")
    account = BrokerAccount(
        user_id=user.id,
        broker=body.broker.value,
        label=body.label,
        environment=body.environment.value,
        credential_ref=body.credential_ref,
        broker_client_id=body.broker_client_id,
        status="connected" if body.broker == Broker.PAPER else "disconnected",
    )
    db.add(account)
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=user.id,
        entity_type="broker_account",
        payload={"action": "create", "broker": body.broker.value, "label": body.label},
    )
    await db.commit()
    return _account_out(account)


@router.delete("/accounts/{account_id}", status_code=204)
async def delete_account(account_id: uuid.UUID, user: VerifiedUser, db: DbSession):
    account = await broker_service.get_account(db, user.id, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    if account.environment == "paper":
        from app.engines.paper.ledger import lock_account

        await lock_account(db, account.id)
        exists = (
            await db.execute(
                select(CashLedger.id).where(CashLedger.broker_account_id == account.id).limit(1)
            )
        ).scalar_one_or_none()
        if exists is not None:
            raise HTTPException(409, "Account has immutable cash history; disconnect it instead")
    await db.delete(account)
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=user.id,
        entity_type="broker_account",
        entity_id=account_id,
        payload={"action": "delete"},
    )
    await db.commit()


@router.post("/accounts/{account_id}/connect")
async def connect_account(account_id: uuid.UUID, user: VerifiedUser, db: DbSession):
    account = await broker_service.get_account(db, user.id, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    try:
        result = await broker_service.connect(db, account)
    except BrokerError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    # Zerodha round-trips opaque redirect_params back to our callback. Mint a
    # single-use state token rather than sending the account id alone: the
    # callback cannot authenticate its caller, so the state is what proves the
    # returning browser is finishing the flow this user started.
    if "login_url" in result and account.broker == Broker.ZERODHA.value:
        state = await oauth_state.issue(
            get_redis(),
            user_id=user.id,
            account_id=account.id,
            broker=account.broker,
        )
        params = urlencode({"account_id": str(account.id), "state": state})
        result["login_url"] += f"&redirect_params={quote(params, safe='')}"
    return result


@router.post("/accounts/{account_id}/verify")
async def verify_read_access(account_id: uuid.UUID, user: VerifiedUser, db: DbSession):
    """Exercise profile + funds against the broker; stamps read_verified_at
    on success. This is the only path that may claim 'read-path verified'."""
    account = await broker_service.get_account(db, user.id, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return await broker_service.verify_read_access(db, account)


@router.post("/accounts/{account_id}/disconnect", response_model=AccountOut)
async def disconnect_account(account_id: uuid.UUID, user: VerifiedUser, db: DbSession):
    account = await broker_service.get_account(db, user.id, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    await broker_service.disconnect(db, account)
    return _account_out(account)


class SessionTokenBody(BaseModel):
    token: str = Field(min_length=8, max_length=512)
    expires_at: datetime | None = None


@router.put("/accounts/{account_id}/session-token", response_model=AccountOut)
async def set_session_token(
    account_id: uuid.UUID, body: SessionTokenBody, user: VerifiedUser, db: DbSession
):
    """Store a broker session/access token (Groww daily token, Breeze session
    token). Encrypted at rest; never echoed back — responses carry masked
    metadata only. API keys/secrets stay in environment variables."""
    account = await broker_service.get_account(db, user.id, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    if account.broker == Broker.PAPER.value:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Paper needs no token")
    await broker_service.store_session_token(db, account, body.token, body.expires_at)
    return _account_out(account)


class LiveEnabledBody(BaseModel):
    live_enabled: bool


@router.patch("/accounts/{account_id}/live", response_model=AccountOut)
async def set_live_enabled(
    account_id: uuid.UUID, body: LiveEnabledBody, user: VerifiedUser, db: DbSession
):
    """Account-level live gate. Disabling is always allowed; enabling must
    pass the same constraints the order pipeline enforces."""
    account = await broker_service.get_account(db, user.id, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    if body.live_enabled:
        refusal = broker_service.can_enable_live(account)
        if refusal:
            raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot enable live: {refusal}")
    account.live_enabled = body.live_enabled
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=user.id,
        entity_type="broker_account",
        entity_id=account.id,
        payload={"action": "set_live_enabled", "live_enabled": body.live_enabled},
    )
    await db.commit()
    return _account_out(account)


@router.post("/accounts/{account_id}/refresh-session")
async def refresh_session(account_id: uuid.UUID, user: VerifiedUser, db: DbSession):
    account = await broker_service.get_account(db, user.id, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return await broker_service.refresh_session(db, account)


@router.post("/accounts/{account_id}/sync")
async def sync_account(account_id: uuid.UUID, user: VerifiedUser, db: DbSession):
    account = await broker_service.get_account(db, user.id, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return await broker_service.sync_snapshots(db, account)


@router.get("/zerodha/callback")
async def zerodha_callback(
    db: DbSession,
    request_token: str | None = None,
    account_id: uuid.UUID | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Kite Connect redirect target.

    Unauthenticated by necessity: Kite redirects the browser here and there is
    no session on the request. The state token minted at /connect is what
    stands in for that -- it is single-use, expires in fifteen minutes, and
    names the account and user it was minted for. Without it, an account id in
    the query string would be enough for anyone to bind their own Kite session
    to someone else's account, or a victim's to their own.

    The account is resolved FROM the state, never from the query string.
    """
    web = get_settings().web_base_url
    if error or not request_token:
        return RedirectResponse(f"{web}/brokers?error=zerodha_auth_failed")

    try:
        claim = await oauth_state.consume(get_redis(), state)
    except oauth_state.StateError:
        # Covers a missing, expired, replayed or forged state. The message is
        # deliberately the same for all of them.
        return RedirectResponse(f"{web}/brokers?error=invalid_state")

    if account_id is not None and not oauth_state.matches(
        claim, account_id=account_id, broker=Broker.ZERODHA.value
    ):
        return RedirectResponse(f"{web}/brokers?error=invalid_state")

    claimed_account_id = uuid.UUID(claim["account_id"])
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.id == claimed_account_id,
            # The state also names the user it was minted for, so a state
            # stolen from one user cannot bind an account belonging to another.
            BrokerAccount.user_id == uuid.UUID(claim["user_id"]),
        )
    )
    account = result.scalar_one_or_none()
    if account is None or account.broker != Broker.ZERODHA.value:
        return RedirectResponse(f"{web}/brokers?error=unknown_account")

    adapter = get_adapter(account)
    assert isinstance(adapter, ZerodhaAdapter)
    try:
        session_data = await adapter.exchange_request_token(request_token)
    except BrokerError:
        account.status = "error"
        account.status_message = "Kite token exchange failed"
        await db.commit()
        return RedirectResponse(f"{web}/brokers?error=token_exchange_failed")

    await broker_service.store_session_token(db, account, session_data["access_token"])
    return RedirectResponse(f"{web}/brokers?connected={account.id}")
