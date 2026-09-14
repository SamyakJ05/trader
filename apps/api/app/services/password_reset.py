"""Self-service password reset.

Same shape as invites: a single-use token whose hash is stored and whose raw
value exists only in the email. Consumption is one atomic UPDATE, so two
concurrent submissions of the same link cannot both succeed.

A reset is the response to a suspected compromise, so completing one revokes
every session for the account — leaving old sessions alive would defeat the
point of resetting.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_token, new_invite_token
from app.db.models import PasswordReset, User

TTL_MINUTES = 30


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def create(db: AsyncSession, user_id: uuid.UUID) -> str:
    # Retire outstanding tokens: five requests should not leave five live
    # links, each valid for half an hour after the others were used.
    await db.execute(
        update(PasswordReset)
        .where(PasswordReset.user_id == user_id, PasswordReset.used_at.is_(None))
        .values(used_at=func.now())
    )
    token = new_invite_token()
    db.add(
        PasswordReset(
            user_id=user_id,
            token_hash=hash_token(token),
            expires_at=utcnow() + timedelta(minutes=TTL_MINUTES),
        )
    )
    await db.flush()
    return token


async def consume(db: AsyncSession, token: str) -> PasswordReset | None:
    """Atomically claim the reset token, or return None if unusable."""
    result = await db.execute(
        update(PasswordReset)
        .where(
            PasswordReset.token_hash == hash_token(token),
            PasswordReset.used_at.is_(None),
            PasswordReset.expires_at > func.now(),
        )
        .values(used_at=func.now())
        .returning(PasswordReset)
    )
    return result.scalar_one_or_none()


async def find_user(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email.lower()))
    return result.scalar_one_or_none()


def reset_url(token: str) -> str:
    return f"{get_settings().web_base_url}/reset-password?token={token}"


def reset_email_body(token: str) -> str:
    return (
        "Someone asked to reset the password on your trader account.\n\n"
        f"{reset_url(token)}\n\n"
        f"The link expires in {TTL_MINUTES} minutes and can only be used once. "
        "Resetting your password signs you out on every device.\n\n"
        "If this wasn't you, ignore this email — your password has not changed."
    )
