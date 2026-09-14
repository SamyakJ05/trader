"""Signed state for broker OAuth redirects.

The broker sends the user's browser back to a callback we cannot authenticate:
there is no session cookie on that request, and the redirect is attacker-
reachable by construction. Binding the returned credential to an account by a
bare account id in the query string means anyone who learns or guesses a UUID
can attach their own broker session to someone else's account, or attach a
victim's session to theirs.

The state token closes that. It is minted when an authenticated user starts the
connect flow, records which account and which user it was minted for, and is
consumed on the way back. A callback carrying no state, an expired state, or a
state naming a different account is refused.

Redis rather than Postgres for the same reason as login challenges: these are
ephemeral by nature, and an expiry the datastore enforces cannot be forgotten
by application code.
"""

import json
import uuid

import redis.asyncio as aioredis

from app.core.security import new_invite_token

# Long enough for a broker login with a 2FA step and a slow phone, short enough
# that a leaked URL in browser history is not a standing liability.
STATE_TTL_SECONDS = 900
_KEY = "oauthstate:{token}"


class StateError(Exception):
    """Raised when a callback's state cannot be trusted."""


async def issue(
    redis: aioredis.Redis,
    *,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    broker: str,
) -> str:
    token = new_invite_token()
    await redis.setex(
        _KEY.format(token=token),
        STATE_TTL_SECONDS,
        json.dumps(
            {
                "user_id": str(user_id),
                "account_id": str(account_id),
                "broker": broker,
            }
        ),
    )
    return token


async def consume(redis: aioredis.Redis, token: str | None) -> dict:
    """Redeem a state token exactly once.

    Consumed rather than merely read: a state that survived its callback could
    be replayed to rebind an account later, and the flow only ever needs it
    once. DELETE returning the value makes the redemption atomic, so two
    concurrent callbacks cannot both succeed on one token.
    """
    if not token:
        raise StateError("Missing state")
    raw = await redis.getdel(_KEY.format(token=token))
    if raw is None:
        raise StateError("State is unknown or has expired")
    try:
        payload = json.loads(raw)
    except ValueError as exc:  # pragma: no cover - only on a corrupted value
        raise StateError("State is malformed") from exc
    if not {"user_id", "account_id", "broker"} <= payload.keys():
        raise StateError("State is malformed")
    return payload


def matches(payload: dict, *, account_id: uuid.UUID, broker: str) -> bool:
    """Whether a redeemed state authorises binding this account.

    The account id is taken FROM the state, not from the query string, so this
    is a consistency check rather than the security boundary — but it catches a
    callback whose query string disagrees with what was minted, which should
    never happen and is worth refusing loudly.
    """
    return payload.get("account_id") == str(account_id) and payload.get("broker") == broker
