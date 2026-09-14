"""Short-lived login challenges.

Login is two-step: a correct password yields a challenge, not a session. The
challenge is exchanged for a session only by presenting a valid TOTP code or
recovery code.

Challenges live in Redis with a hard TTL rather than in Postgres — they are
ephemeral by nature, and an expiry that the datastore enforces cannot be
forgotten by application code. A challenge authorises exactly one thing:
completing second-factor verification for the user it names.
"""

import uuid

import redis.asyncio as aioredis

from app.core.security import new_invite_token

CHALLENGE_TTL_SECONDS = 300
_KEY = "authchallenge:{token}"


def _user_index(user_id: uuid.UUID) -> str:
    return f"authchallenge:user:{user_id}"


async def issue(redis: aioredis.Redis, user_id: uuid.UUID) -> str:
    token = new_invite_token()
    key = _KEY.format(token=token)
    await redis.setex(key, CHALLENGE_TTL_SECONDS, str(user_id))
    # Indexed by user so a password change can revoke outstanding challenges.
    await redis.sadd(_user_index(user_id), token)
    await redis.expire(_user_index(user_id), CHALLENGE_TTL_SECONDS)
    return token


async def resolve(redis: aioredis.Redis, token: str) -> uuid.UUID | None:
    """Returns the user the challenge names, without consuming it.

    Not consumed on read so a mistyped TOTP code does not force the user back
    to the password step; the rate limiter bounds the retries instead.
    """
    if not token:
        return None
    value = await redis.get(_KEY.format(token=token))
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def revoke_all_for_user(redis: aioredis.Redis, user_id: uuid.UUID) -> None:
    """Invalidate every outstanding challenge for a user.

    Called on password change and reset. Without this, someone who phished the
    old password and holds a live challenge can still complete it — with a
    stolen code — for up to five minutes after the victim resets, which is
    exactly the window the reset is meant to close.
    """
    index = _user_index(user_id)
    tokens = await redis.smembers(index)
    for token in tokens:
        await redis.delete(_KEY.format(token=token))
    await redis.delete(index)


async def consume(redis: aioredis.Redis, token: str) -> None:
    """Called once the challenge has been exchanged for a session."""
    value = await redis.get(_KEY.format(token=token))
    await redis.delete(_KEY.format(token=token))
    if value:
        await redis.srem(_user_index(uuid.UUID(value)), token)
