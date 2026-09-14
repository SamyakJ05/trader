import re
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.deps import CurrentSession, CurrentUser, DbSession
from app.core.redis import get_redis
from app.core.security import hash_password, verify_password
from app.db.models import User
from app.domain.enums import AuditEventType
from app.services import audit, ratelimit
from app.services import invites as invite_service
from app.services import sessions as session_service


EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_user_email(value: str) -> str:
    if not EMAIL_REGEX.match(value):
        raise ValueError("value is not a valid email address")
    return value.lower()

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    """Registration is invite-only: the token carries the address, so the
    client cannot choose which account it is creating."""

    token: str
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return validate_user_email(value)


class UserOut(BaseModel):
    id: str
    email: str
    full_name: str | None
    is_admin: bool = False


class AuthResponse(BaseModel):
    token: str
    expires_at: datetime
    user: UserOut


async def _create_session(
    db: DbSession, user: User, request: Request | None = None
) -> AuthResponse:
    session = await session_service.create(
        db,
        user.id,
        user_agent=request.headers.get("user-agent") if request else None,
        ip=request.client.host if request and request.client else None,
    )
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=user.id,
        entity_type="session",
        entity_id=session.id,
        payload={"action": "login"},
    )
    await db.commit()
    return AuthResponse(
        token=session.token,
        expires_at=session.expires_at,
        user=UserOut(
            id=str(user.id),
            email=user.email,
            full_name=user.full_name,
            is_admin=user.is_admin,
        ),
    )


class InvitePreview(BaseModel):
    email: str
    full_name: str | None = None


@router.get("/invite/{token}", response_model=InvitePreview)
async def preview_invite(token: str, db: DbSession):
    """Lets the signup page show which address it is claiming, before the
    invitee commits a password."""
    invite = await invite_service.lookup(db, token)
    error = invite_service.invite_error(invite)
    if error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, error)
    return InvitePreview(email=invite.email, full_name=invite.full_name)


@router.post("/register", response_model=AuthResponse, status_code=201)
async def register(body: RegisterRequest, request: Request, db: DbSession):
    # Claim the invite atomically. A read-then-write would let two concurrent
    # submissions of the same token both pass validation.
    invite = await invite_service.consume(db, body.token)
    if invite is None:
        # Distinguish "never existed" from "no longer claimable" for the
        # message only; either way nothing was consumed.
        error = invite_service.invite_error(await invite_service.lookup(db, body.token))
        raise HTTPException(status.HTTP_403_FORBIDDEN, error or "That invite is no longer valid")

    user = User(
        email=invite.email,
        password_hash=hash_password(body.password),
        full_name=body.full_name or invite.full_name,
        is_admin=invite.as_admin,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        # The unique index on users.email is the backstop if an account was
        # created for this address by another route in the meantime.
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Email already registered"
        ) from exc
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        payload={"action": "register", "via": "invite", "invite_id": str(invite.id)},
    )
    return await _create_session(db, user, request)


@router.post("/login", response_model=AuthResponse)
async def login(body: LoginRequest, request: Request, db: DbSession):
    redis = get_redis()
    ip = request.client.host if request.client else "unknown"
    # Limited on both axes: by email so one account cannot be ground down, and
    # by IP so one source cannot sweep many accounts.
    for scope, identifier in (("login", body.email), ("login-ip", ip)):
        try:
            await ratelimit.check(redis, scope, identifier)
        except ratelimit.RateLimitExceeded as exc:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many failed attempts. Try again later.",
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc

    result = await db.execute(select(User).where(User.email == body.email.lower()))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        await ratelimit.record_failure(redis, "login", body.email)
        await ratelimit.record_failure(redis, "login-ip", ip)
        # Same message either way: a distinct "no such account" reply would
        # turn this endpoint into an account-existence oracle.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is suspended")

    # Clear the email axis only. Clearing the IP axis on success would let an
    # attacker holding any one valid login reset it at will, neutralising the
    # axis that exists to stop one source sweeping many accounts. The IP
    # counter decays on its own TTL instead.
    await ratelimit.clear(redis, "login", body.email)
    return await _create_session(db, user, request)


@router.post("/logout", status_code=204)
async def logout(user: CurrentUser, session: CurrentSession, db: DbSession):
    """Revokes only the session that made this request. Signing out on a phone
    must not sign the user out on their desktop."""
    await session_service.revoke(db, session)
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=user.id,
        entity_type="session",
        entity_id=session.id,
        payload={"action": "logout"},
    )
    await db.commit()


class SessionOut(BaseModel):
    id: str
    user_agent: str | None
    ip: str | None
    created_at: datetime
    last_seen_at: datetime | None
    current: bool


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(user: CurrentUser, session: CurrentSession, db: DbSession):
    rows = await session_service.list_for_user(db, user.id)
    return [
        SessionOut(
            id=str(s.id),
            user_agent=s.user_agent,
            ip=s.ip,
            created_at=s.created_at,
            last_seen_at=s.last_seen_at,
            current=s.id == session.id,
        )
        for s in rows
    ]


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke_session(
    session_id: uuid.UUID, user: CurrentUser, session: CurrentSession, db: DbSession
):
    target = await session_service.owned(db, user.id, session_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    await session_service.revoke(db, target)
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=user.id,
        entity_type="session",
        entity_id=target.id,
        payload={
            "action": "revoke_session",
            "self": target.id == session.id,
        },
    )
    await db.commit()


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser):
    return UserOut(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        is_admin=user.is_admin,
    )
