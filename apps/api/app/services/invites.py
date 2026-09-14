"""Invite-only registration.

Registration consumes an invite rather than creating an account from nothing.
Clicking the emailed link proves control of the address, so an invited account
is email-verified by construction — there is no separate verification flow.

The raw token exists only in the email; the database stores its hash.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_token, new_invite_token
from app.db.models import Invite, User


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def invite_error(invite: Invite | None) -> str | None:
    """Returns a refusal reason, or None if the invite may be consumed.

    Split out from the route so every rule is unit-testable without a database
    and so the same checks apply wherever an invite is redeemed.
    """
    if invite is None:
        return "That invite link is not valid"
    if invite.revoked_at is not None:
        return "That invite has been revoked"
    if invite.consumed_at is not None:
        return "That invite has already been used"
    expires_at = invite.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < utcnow():
        return "That invite has expired"
    return None


async def create(
    db: AsyncSession,
    *,
    email: str,
    invited_by: uuid.UUID,
    full_name: str | None = None,
    as_admin: bool = False,
) -> tuple[Invite, str]:
    """Creates an invite and returns it with the raw token (emailed once)."""
    token = new_invite_token()
    invite = Invite(
        email=email.lower(),
        token_hash=hash_token(token),
        invited_by=invited_by,
        full_name=full_name,
        as_admin=as_admin,
        expires_at=utcnow() + timedelta(hours=get_settings().invite_ttl_hours),
    )
    db.add(invite)
    await db.flush()
    return invite, token


async def lookup(db: AsyncSession, token: str) -> Invite | None:
    """Read-only lookup, for previewing an invite before it is consumed.

    Do NOT use this to gate registration: a read-then-write lets two concurrent
    submissions of the same token both pass. Use `consume` instead.
    """
    result = await db.execute(select(Invite).where(Invite.token_hash == hash_token(token)))
    return result.scalar_one_or_none()


async def consume(db: AsyncSession, token: str) -> Invite | None:
    """Atomically claim an invite, or return None if it is not claimable.

    The database, not application-level check ordering, is the arbiter: the
    WHERE clause re-states every validity rule, so under READ COMMITTED only
    one of two concurrent registrations with the same token can match a row.
    A Python-side check followed by a separate write would let both through.
    """
    result = await db.execute(
        update(Invite)
        .where(
            Invite.token_hash == hash_token(token),
            Invite.consumed_at.is_(None),
            Invite.revoked_at.is_(None),
            Invite.expires_at > func.now(),
        )
        .values(consumed_at=func.now())
        .returning(Invite)
    )
    return result.scalar_one_or_none()


async def email_taken(db: AsyncSession, email: str) -> bool:
    result = await db.execute(select(User).where(User.email == email.lower()))
    return result.scalar_one_or_none() is not None


def invite_url(token: str) -> str:
    return f"{get_settings().web_base_url}/invite?token={token}"


def invite_email_body(inviter_email: str, token: str) -> str:
    return (
        f"{inviter_email} invited you to their trader instance.\n\n"
        f"Set your password to finish creating your account:\n"
        f"{invite_url(token)}\n\n"
        f"The link expires in {get_settings().invite_ttl_hours // 24} days and "
        f"can only be used once.\n\n"
        "If you were not expecting this, you can ignore it."
    )
