import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.deps import CurrentSession, DbSession, EnrollingUser, FullSessionUser
from app.core.redis import get_redis
from app.core.security import hash_password, verify_password
from app.db.models import User
from app.domain.enums import AuditEventType
from app.services import audit, challenge, email, ratelimit, risk_defaults
from app.services import invites as invite_service
from app.services import password_reset as reset_service
from app.services import sessions as session_service
from app.services import totp as totp_service

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
    totp_enabled: bool = False


class AuthResponse(BaseModel):
    token: str
    expires_at: datetime
    user: UserOut
    # True when this session may only complete TOTP enrolment.
    enrolment_only: bool = False


class ChallengeResponse(BaseModel):
    """Returned by /login. A correct password yields a challenge, not a
    session: the challenge is exchanged for one at /login/verify."""

    challenge: str
    expires_in: int = challenge.CHALLENGE_TTL_SECONDS
    totp_enrolled: bool


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        is_admin=user.is_admin,
        totp_enabled=totp_service.is_enrolled(user),
    )


async def _create_session(
    db: DbSession,
    user: User,
    request: Request | None = None,
    *,
    enrolment_only: bool = False,
) -> AuthResponse:
    session = await session_service.create(
        db,
        user.id,
        user_agent=request.headers.get("user-agent") if request else None,
        ip=request.client.host if request and request.client else None,
        enrolment_only=enrolment_only,
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
        user=_user_out(user),
        enrolment_only=enrolment_only,
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
    # The risk engine loops over ENABLED rules, so a user with none passes
    # every check. Only the demo seeder created any, and a production deploy
    # never runs it -- which left a real account with no limit of any kind.
    rules = await risk_defaults.provision(db, user.id)
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        payload={
            "action": "register",
            "via": "invite",
            "invite_id": str(invite.id),
            "risk_rules_provisioned": rules,
        },
    )
    return await _create_session(db, user, request)


@router.post("/login", response_model=ChallengeResponse)
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

    # Password alone does not authenticate. It buys a short-lived challenge
    # that only a second factor can exchange for a session.
    token = await challenge.issue(redis, user.id)
    await db.commit()
    return ChallengeResponse(
        challenge=token, totp_enrolled=totp_service.is_enrolled(user)
    )


class VerifyBody(BaseModel):
    challenge: str
    code: str | None = None
    recovery_code: str | None = None


@router.post("/login/verify", response_model=AuthResponse)
async def verify_login(body: VerifyBody, request: Request, db: DbSession):
    """Exchanges a login challenge for a session by proving the second factor.

    Accepts either a TOTP code or a recovery code. Both are rate limited: a
    TOTP code is six digits, so an unlimited verify endpoint would make the
    second factor a formality.
    """
    redis = get_redis()
    user_id = await challenge.resolve(redis, body.challenge)
    if user_id is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "That login attempt expired — sign in again"
        )

    scope = f"totp:{user_id}"
    try:
        await ratelimit.check(redis, scope, str(user_id))
    except ratelimit.RateLimitExceeded as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed codes. Try again later.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")

    secret = totp_service.user_secret(user)
    if not totp_service.is_enrolled(user) or secret is None:
        # No second factor to present yet — which is the state of every
        # account on an instance that has just made TOTP mandatory, including
        # the operator's. Refusing here would lock everyone out with no way
        # back in, since enrolment itself needs a session. Issue one scoped to
        # enrolment only: short-lived, and rejected by every route except the
        # setup endpoints.
        await challenge.consume(redis, body.challenge)
        return await _create_session(db, user, request, enrolment_only=True)

    verified = False
    used_recovery = False
    if body.code:
        # verify_code_once, not verify_code: a TOTP code is valid for ~90s, so
        # an observed code must not be replayable for the rest of that window.
        verified = await totp_service.verify_code_once(redis, user.id, secret, body.code)
    if not verified and body.recovery_code:
        verified = await totp_service.consume_recovery_code(
            db, user.id, body.recovery_code
        )
        used_recovery = verified

    if not verified:
        await ratelimit.record_failure(redis, scope, str(user_id))
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That code is not valid")

    await ratelimit.clear(redis, scope, str(user_id))
    await challenge.consume(redis, body.challenge)
    if used_recovery:
        remaining = await totp_service.remaining_recovery_codes(db, user.id)
        await audit.emit(
            db,
            AuditEventType.USER_ACTION,
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            payload={"action": "recovery_code_used", "remaining": remaining},
        )
    return await _create_session(db, user, request)


@router.post("/logout", status_code=204)
async def logout(user: EnrollingUser, session: CurrentSession, db: DbSession):
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
async def list_sessions(user: FullSessionUser, session: CurrentSession, db: DbSession):
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
    session_id: uuid.UUID, user: FullSessionUser, session: CurrentSession, db: DbSession
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
async def me(user: EnrollingUser):
    return _user_out(user)


# ── TOTP enrolment ───────────────────────────────────────────────────
# These routes use CurrentUser, not VerifiedUser: they must be reachable by a
# user who has not yet enrolled, which is the whole point.


class TotpSetupOut(BaseModel):
    secret: str
    provisioning_uri: str


class TotpEnableBody(BaseModel):
    code: str


class TotpEnabledOut(BaseModel):
    recovery_codes: list[str]


@router.post("/totp/setup", response_model=TotpSetupOut)
async def totp_setup(user: EnrollingUser, db: DbSession):
    """Issues a secret and its QR provisioning URI.

    The secret is stored immediately but not yet active — totp_enabled_at is
    set only once the user proves they can generate a code from it, so a failed
    enrolment cannot lock them out of their own account.
    """
    if totp_service.is_enrolled(user):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Two-factor authentication is already set up"
        )
    secret = totp_service.new_secret()
    totp_service.store_secret(user, secret)
    await db.commit()
    return TotpSetupOut(
        secret=secret,
        provisioning_uri=totp_service.provisioning_uri(secret, user.email),
    )


@router.post("/totp/enable", response_model=TotpEnabledOut)
async def totp_enable(
    body: TotpEnableBody, user: EnrollingUser, session: CurrentSession, db: DbSession
):
    if totp_service.is_enrolled(user):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Two-factor authentication is already set up"
        )
    secret = totp_service.user_secret(user)
    if secret is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Start setup first to get a secret"
        )
    if not await totp_service.verify_code_once(get_redis(), user.id, secret, body.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That code is not valid")

    user.totp_enabled_at = datetime.now(timezone.utc)
    # The session that just finished enrolment must become a full session in
    # the same request: the frontend navigates onward using this same token,
    # and without this it would immediately 403 as totp_setup_required on any
    # route past setup — a redirect loop back to a page that now correctly
    # refuses, because the account really is enrolled. Same token, no new one
    # to issue: enrolment_only is the only thing standing between it and a
    # route gated on FullSessionUser.
    session.enrolment_only = False
    codes = totp_service.generate_recovery_codes()
    await totp_service.replace_recovery_codes(db, user.id, codes)
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        payload={"action": "totp_enabled"},
    )
    await db.commit()
    # Shown exactly once — they are hashed at rest and cannot be re-displayed.
    return TotpEnabledOut(recovery_codes=codes)


class RecoveryStatusOut(BaseModel):
    totp_enabled: bool
    recovery_codes_remaining: int


@router.get("/totp/status", response_model=RecoveryStatusOut)
async def totp_status(user: EnrollingUser, db: DbSession):
    return RecoveryStatusOut(
        totp_enabled=totp_service.is_enrolled(user),
        recovery_codes_remaining=await totp_service.remaining_recovery_codes(db, user.id),
    )


@router.post("/totp/recovery-codes", response_model=TotpEnabledOut)
async def regenerate_recovery_codes(body: TotpEnableBody, user: FullSessionUser, db: DbSession):
    """Re-issues the full set, invalidating the old one. Requires a current
    TOTP code so a hijacked session cannot mint itself fresh backup codes."""
    secret = totp_service.user_secret(user)
    if not totp_service.is_enrolled(user) or secret is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Two-factor is not set up")
    # One-shot: this endpoint mints new credentials, so a replayed code must
    # not be able to do it twice.
    if not await totp_service.verify_code_once(get_redis(), user.id, secret, body.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That code is not valid")
    codes = totp_service.generate_recovery_codes()
    await totp_service.replace_recovery_codes(db, user.id, codes)
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        payload={"action": "recovery_codes_regenerated"},
    )
    await db.commit()
    return TotpEnabledOut(recovery_codes=codes)


# ── password reset ───────────────────────────────────────────────────


class ForgotBody(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return validate_user_email(value)


class ResetBody(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=128)


@router.post("/password/forgot", status_code=202)
async def forgot_password(body: ForgotBody, request: Request, db: DbSession):
    """Always returns 202, whether or not the address exists — a distinct
    reply would turn this into an account-existence oracle."""
    redis = get_redis()
    ip = request.client.host if request.client else "unknown"
    # Two axes: the email axis stops one attacker rotating IPs from flooding a
    # victim's inbox; the IP axis (deliberately looser) stops one source
    # sweeping many addresses without penalising a shared NAT.
    for scope, identifier, limit in (
        ("reset-email", body.email, 3),
        ("reset-ip", ip, 20),
    ):
        try:
            await ratelimit.check(redis, scope, identifier, limit=limit)
        except ratelimit.RateLimitExceeded as exc:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many requests. Try again later.",
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc
    # Counted on every request, not only failures: this endpoint has no
    # success signal to clear on, since it answers identically either way.
    await ratelimit.record_failure(redis, "reset-email", body.email)
    await ratelimit.record_failure(redis, "reset-ip", ip)

    user = await reset_service.find_user(db, body.email)
    if user is not None and user.is_active:
        token = await reset_service.create(db, user.id)
        await audit.emit(
            db,
            AuditEventType.USER_ACTION,
            user_id=user.id,
            entity_type="user",
            entity_id=user.id,
            payload={"action": "password_reset_requested"},
        )
        await db.commit()
        await email.send(
            user.email,
            "Reset your trader password",
            reset_service.reset_email_body(token),
        )
    return {"status": "accepted"}


@router.post("/password/reset", status_code=204)
async def reset_password(body: ResetBody, db: DbSession):
    record = await reset_service.consume(db, body.token)
    if record is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "That reset link is no longer valid"
        )
    user = await db.get(User, record.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "That reset link is no longer valid")

    user.password_hash = hash_password(body.password)
    user.password_changed_at = datetime.now(timezone.utc)
    # A reset is the response to a suspected compromise: leaving existing
    # sessions — or outstanding login challenges bought with the old password —
    # alive would defeat the point.
    await session_service.revoke_all_for_user(db, user.id)
    await challenge.revoke_all_for_user(get_redis(), user.id)
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        payload={"action": "password_reset_completed", "sessions_revoked": True},
    )
    await db.commit()


class ChangePasswordBody(BaseModel):
    current_password: str
    password: str = Field(min_length=8, max_length=128)


@router.post("/password/change", status_code=204)
async def change_password(
    body: ChangePasswordBody, user: FullSessionUser, session: CurrentSession, db: DbSession
):
    """Changing a password signs out every OTHER device, keeping the current
    one — otherwise a routine password change logs you out of the session you
    are using to make it."""
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is incorrect")
    user.password_hash = hash_password(body.password)
    user.password_changed_at = datetime.now(timezone.utc)
    await session_service.revoke_all_for_user(db, user.id, except_session_id=session.id)
    await challenge.revoke_all_for_user(get_redis(), user.id)
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        payload={"action": "password_changed"},
    )
    await db.commit()
