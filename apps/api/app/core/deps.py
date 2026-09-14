from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuthSession, User
from app.db.session import get_session

DbSession = Annotated[AsyncSession, Depends(get_session)]


async def get_current_session(
    db: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthSession:
    """Resolves the bearer token to a live session row.

    Sessions are revoked by setting revoked_at rather than being deleted, so a
    revoked row still exists and must be rejected explicitly — otherwise
    signing out a device would leave its token working.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()

    result = await db.execute(select(AuthSession).where(AuthSession.token == token))
    session = result.scalar_one_or_none()
    if session is None or session.revoked_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session")

    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session")

    # Persisted here rather than left to the route: get_session never commits
    # on its own, so a read-only request would otherwise drop this write and
    # the device list would show stale timestamps. Throttled to once a minute
    # so ordinary polling does not write on every request.
    now = datetime.now(timezone.utc)
    last_seen = session.last_seen_at
    if last_seen is not None and last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    if last_seen is None or (now - last_seen).total_seconds() > 60:
        session.last_seen_at = now
        await db.commit()
    return session


CurrentSession = Annotated[AuthSession, Depends(get_current_session)]


async def get_current_user(db: DbSession, session: CurrentSession) -> User:
    user = await db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User inactive")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_current_admin(user: CurrentUser) -> User:
    """Operator-only dependency. Use for actions whose effect crosses tenant
    boundaries; ordinary per-user actions must never require it."""
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrator privileges required")
    return user


CurrentAdmin = Annotated[User, Depends(get_current_admin)]
