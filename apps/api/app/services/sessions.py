"""Session lifecycle.

Sessions are device records: revocation sets revoked_at rather than deleting
the row, so a signed-out device stays visible in the user's device list and in
the audit trail.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import new_session_token
from app.db.models import AuthSession


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_live(session) -> bool:
    """A session authenticates a request only while unrevoked and unexpired."""
    if session.revoked_at is not None:
        return False
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at >= utcnow()


# An enrolment session exists only to finish TOTP setup. It is short-lived and
# its holder has proven a password but no second factor, so it must not be
# usable for anything else.
ENROLMENT_TTL_MINUTES = 15


async def create(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    user_agent: str | None = None,
    ip: str | None = None,
    enrolment_only: bool = False,
) -> AuthSession:
    now = utcnow()
    ttl = (
        timedelta(minutes=ENROLMENT_TTL_MINUTES)
        if enrolment_only
        else timedelta(hours=get_settings().session_ttl_hours)
    )
    session = AuthSession(
        user_id=user_id,
        token=new_session_token(),
        expires_at=now + ttl,
        user_agent=(user_agent or "")[:512] or None,
        ip=ip,
        last_seen_at=now,
        enrolment_only=enrolment_only,
    )
    db.add(session)
    await db.flush()
    return session


async def revoke(db: AsyncSession, session: AuthSession) -> None:
    if session.revoked_at is None:
        session.revoked_at = utcnow()


async def revoke_all_for_user(
    db: AsyncSession, user_id: uuid.UUID, *, except_session_id: uuid.UUID | None = None
) -> None:
    query = update(AuthSession).where(
        AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)
    )
    if except_session_id is not None:
        query = query.where(AuthSession.id != except_session_id)
    await db.execute(query.values(revoked_at=utcnow()))


async def list_for_user(db: AsyncSession, user_id: uuid.UUID) -> list[AuthSession]:
    result = await db.execute(
        select(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .order_by(AuthSession.last_seen_at.desc().nullslast())
    )
    return [s for s in result.scalars() if is_live(s)]


async def owned(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID
) -> AuthSession | None:
    """Ownership-scoped lookup, per the phase 1a convention: never load a
    client-supplied id without filtering by the caller."""
    result = await db.execute(
        select(AuthSession).where(
            AuthSession.id == session_id, AuthSession.user_id == user_id
        )
    )
    return result.scalar_one_or_none()
