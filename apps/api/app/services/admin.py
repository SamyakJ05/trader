"""Operator actions over other users' accounts.

Every function here crosses the tenant boundary by design, so each one is
reachable only through the CurrentAdmin dependency. The guards below exist
because an instance with no usable admin has no reachable global kill switch —
the break-glass control that halts trading for everyone.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuthSession, User


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def demote(db: AsyncSession, target_id: uuid.UUID) -> bool:
    """Atomically clear is_admin only while another active admin remains.

    A Python-side count followed by a separate write is racy: two operators
    demoting each other simultaneously both read count == 2, both pass, and
    the instance ends with zero admins — the unadministrable state the guard
    exists to prevent. The subquery re-checks inside the same statement.
    """
    other_admins = (
        select(func.count())
        .select_from(User)
        .where(User.is_admin, User.is_active, User.id != target_id)
        .scalar_subquery()
    )
    result = await db.execute(
        update(User)
        .where(User.id == target_id, other_admins > 0)
        .values(is_admin=False)
        .returning(User.id)
    )
    return result.scalar_one_or_none() is not None


async def suspend(db: AsyncSession, target_id: uuid.UUID) -> bool:
    """Atomically deactivate, refusing if it would remove the last operator."""
    other_admins = (
        select(func.count())
        .select_from(User)
        .where(User.is_admin, User.is_active, User.id != target_id)
        .scalar_subquery()
    )
    result = await db.execute(
        update(User)
        .where(
            User.id == target_id,
            # An ordinary user can always be suspended; an admin only while
            # another active admin remains.
            (~User.is_admin) | (other_admins > 0),
        )
        .values(is_active=False)
        .returning(User.id)
    )
    return result.scalar_one_or_none() is not None


async def revoke_all_sessions(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Used when suspending an account: an is_active flag alone would leave
    existing bearer tokens working until they expired."""
    await db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
