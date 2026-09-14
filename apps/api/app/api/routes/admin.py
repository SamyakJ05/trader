"""Operator endpoints.

Every route here reaches across the tenant boundary and is gated by
CurrentAdmin. The self-lockout guards in app/services/admin.py are what keep an
operator from making their own instance unadministrable — and, since phase 1a,
the global kill switch operator-only, unreachable.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.api.routes.auth import validate_user_email
from app.core.deps import CurrentAdmin, DbSession
from app.db.models import Invite, User
from app.domain.enums import AuditEventType
from app.services import admin as admin_service
from app.services import audit, email
from app.services import invites as invite_service
from app.services import sessions as session_service
from app.services import totp as totp_service

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminUserOut(BaseModel):
    id: str
    email: str
    full_name: str | None
    is_admin: bool
    is_active: bool
    created_at: datetime
    active_sessions: int
    totp_enabled: bool = False


class InviteBody(BaseModel):
    email: str
    full_name: str | None = None
    as_admin: bool = False

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return validate_user_email(value)


class InviteOut(BaseModel):
    id: str
    email: str
    full_name: str | None
    as_admin: bool
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    revoked_at: datetime | None
    invite_url: str | None = Field(
        default=None,
        description=(
            "Returned only when the invite is created, and only if email "
            "delivery failed — so the operator can pass the link on manually."
        ),
    )


class AdminFlagBody(BaseModel):
    is_admin: bool


@router.get("/users", response_model=list[AdminUserOut])
async def list_users(admin: CurrentAdmin, db: DbSession):
    result = await db.execute(select(User).order_by(User.created_at))
    users = list(result.scalars())
    out = []
    for u in users:
        sessions = await session_service.list_for_user(db, u.id)
        out.append(
            AdminUserOut(
                id=str(u.id),
                email=u.email,
                full_name=u.full_name,
                is_admin=u.is_admin,
                is_active=u.is_active,
                created_at=u.created_at,
                active_sessions=len(sessions),
                totp_enabled=totp_service.is_enrolled(u),
            )
        )
    return out


@router.get("/invites", response_model=list[InviteOut])
async def list_invites(admin: CurrentAdmin, db: DbSession):
    result = await db.execute(select(Invite).order_by(Invite.created_at.desc()).limit(100))
    return [
        InviteOut(
            id=str(i.id),
            email=i.email,
            full_name=i.full_name,
            as_admin=i.as_admin,
            created_at=i.created_at,
            expires_at=i.expires_at,
            consumed_at=i.consumed_at,
            revoked_at=i.revoked_at,
        )
        for i in result.scalars()
    ]


@router.post("/invites", response_model=InviteOut, status_code=201)
async def create_invite(body: InviteBody, admin: CurrentAdmin, db: DbSession):
    if await invite_service.email_taken(db, body.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "That email already has an account")

    invite, token = await invite_service.create(
        db,
        email=body.email,
        invited_by=admin.id,
        full_name=body.full_name,
        as_admin=body.as_admin,
    )
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=admin.id,
        entity_type="invite",
        entity_id=invite.id,
        payload={"action": "invite", "email": body.email, "as_admin": body.as_admin},
    )
    await db.commit()

    # Best-effort: a delivery failure leaves a valid invite the operator can
    # hand over by hand, rather than failing the request and leaving them
    # unsure whether it was created.
    delivered = await email.send(
        body.email,
        "You have been invited to trader",
        invite_service.invite_email_body(admin.email, token),
    )
    return InviteOut(
        id=str(invite.id),
        email=invite.email,
        full_name=invite.full_name,
        as_admin=invite.as_admin,
        created_at=invite.created_at,
        expires_at=invite.expires_at,
        consumed_at=None,
        revoked_at=None,
        invite_url=None if delivered else invite_service.invite_url(token),
    )


@router.delete("/invites/{invite_id}", status_code=204)
async def revoke_invite(invite_id: uuid.UUID, admin: CurrentAdmin, db: DbSession):
    invite = await db.get(Invite, invite_id)
    if invite is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invite not found")
    if invite.consumed_at is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "That invite has already been used"
        )
    invite.revoked_at = admin_service.utcnow()
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=admin.id,
        entity_type="invite",
        entity_id=invite.id,
        payload={"action": "revoke_invite", "email": invite.email},
    )
    await db.commit()


async def _target(db: DbSession, user_id: uuid.UUID) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user


def _guard_self(admin: User, target: User, action: str) -> None:
    """Self-action guard. Not racy — the actor's identity is fixed for the
    request — so it stays a plain Python check. The last-admin rule is
    enforced inside the UPDATE instead, where it cannot race."""
    if admin.id == target.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"You cannot {action} yourself — ask another operator to do it",
        )


@router.post("/users/{user_id}/suspend", response_model=AdminUserOut)
async def suspend_user(user_id: uuid.UUID, admin: CurrentAdmin, db: DbSession):
    target = await _target(db, user_id)
    _guard_self(admin, target, "suspend")
    if not await admin_service.suspend(db, target.id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Cannot suspend the last remaining operator; promote another user first",
        )
    # An is_active flag alone would leave existing bearer tokens working until
    # they expired.
    await admin_service.revoke_all_sessions(db, target.id)
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=admin.id,
        entity_type="user",
        entity_id=target.id,
        payload={"action": "suspend", "email": target.email},
    )
    await db.commit()
    return await _user_out(db, target)


@router.post("/users/{user_id}/unsuspend", response_model=AdminUserOut)
async def unsuspend_user(user_id: uuid.UUID, admin: CurrentAdmin, db: DbSession):
    target = await _target(db, user_id)
    target.is_active = True
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=admin.id,
        entity_type="user",
        entity_id=target.id,
        payload={"action": "unsuspend", "email": target.email},
    )
    await db.commit()
    return await _user_out(db, target)


@router.post("/users/{user_id}/reset-2fa", response_model=AdminUserOut)
async def reset_two_factor(user_id: uuid.UUID, admin: CurrentAdmin, db: DbSession):
    """Clears a user's second factor and recovery codes, forcing re-enrolment.

    This is the lost-authenticator path. TOTP is mandatory, so the user is
    routed straight back to setup on their next request; their sessions are
    revoked so a device that is already signed in cannot skip it.
    """
    target = await _target(db, user_id)
    await totp_service.disable(db, target)
    await admin_service.revoke_all_sessions(db, target.id)
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=admin.id,
        entity_type="user",
        entity_id=target.id,
        payload={"action": "reset_2fa", "email": target.email},
    )
    await db.commit()
    return await _user_out(db, target)


@router.patch("/users/{user_id}/admin", response_model=AdminUserOut)
async def set_admin(
    user_id: uuid.UUID, body: AdminFlagBody, admin: CurrentAdmin, db: DbSession
):
    target = await _target(db, user_id)
    if body.is_admin:
        target.is_admin = True
    else:
        _guard_self(admin, target, "demote")
        if not await admin_service.demote(db, target.id):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Cannot demote the last remaining operator; promote another user first",
            )
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=admin.id,
        entity_type="user",
        entity_id=target.id,
        payload={
            "action": "grant_admin" if body.is_admin else "revoke_admin",
            "email": target.email,
        },
    )
    await db.commit()
    return await _user_out(db, target)


async def _user_out(db: DbSession, user: User) -> AdminUserOut:
    sessions = await session_service.list_for_user(db, user.id)
    return AdminUserOut(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        is_admin=user.is_admin,
        is_active=user.is_active,
        created_at=user.created_at,
        active_sessions=len(sessions),
        totp_enabled=totp_service.is_enrolled(user),
    )
