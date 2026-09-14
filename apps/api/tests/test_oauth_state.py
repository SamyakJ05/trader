"""Broker OAuth callback binding.

The callback cannot authenticate its caller: the broker redirects the user's
browser to it with no session. Before the state token, the account to bind came
from a query parameter, so anyone who learned an account UUID could attach
their own broker session to it — or attach a victim's session to their own
account, which is worse, because the victim's orders would then route through
credentials the attacker controls.
"""

import uuid

import fakeredis.aioredis
import pytest

from app.services import oauth_state


def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def test_a_minted_state_names_its_user_account_and_broker():
    r = redis()
    user_id, account_id = uuid.uuid4(), uuid.uuid4()
    token = await oauth_state.issue(
        r, user_id=user_id, account_id=account_id, broker="zerodha"
    )
    claim = await oauth_state.consume(r, token)
    assert claim["user_id"] == str(user_id)
    assert claim["account_id"] == str(account_id)
    assert claim["broker"] == "zerodha"


async def test_a_state_cannot_be_used_twice():
    """A state that survived its callback could be replayed later to rebind the
    account; the flow only ever needs it once."""
    r = redis()
    token = await oauth_state.issue(
        r, user_id=uuid.uuid4(), account_id=uuid.uuid4(), broker="zerodha"
    )
    await oauth_state.consume(r, token)
    with pytest.raises(oauth_state.StateError):
        await oauth_state.consume(r, token)


async def test_a_missing_state_is_refused():
    """The pre-fix callback accepted a bare account id with no state at all."""
    with pytest.raises(oauth_state.StateError):
        await oauth_state.consume(redis(), None)
    with pytest.raises(oauth_state.StateError):
        await oauth_state.consume(redis(), "")


async def test_an_unknown_state_is_refused():
    """A forged or guessed token names nothing."""
    with pytest.raises(oauth_state.StateError):
        await oauth_state.consume(redis(), "not-a-real-token")


async def test_state_carries_a_hard_expiry():
    """The datastore enforces the TTL, so application code cannot forget it and
    a URL left in browser history stops working."""
    r = redis()
    token = await oauth_state.issue(
        r, user_id=uuid.uuid4(), account_id=uuid.uuid4(), broker="zerodha"
    )
    ttl = await r.ttl(f"oauthstate:{token}")
    assert 0 < ttl <= oauth_state.STATE_TTL_SECONDS


async def test_a_state_minted_for_one_account_does_not_match_another():
    r = redis()
    mine = uuid.uuid4()
    token = await oauth_state.issue(
        r, user_id=uuid.uuid4(), account_id=mine, broker="zerodha"
    )
    claim = await oauth_state.consume(r, token)
    assert oauth_state.matches(claim, account_id=mine, broker="zerodha")
    assert not oauth_state.matches(claim, account_id=uuid.uuid4(), broker="zerodha")


async def test_a_state_minted_for_one_broker_does_not_match_another():
    r = redis()
    account_id = uuid.uuid4()
    token = await oauth_state.issue(
        r, user_id=uuid.uuid4(), account_id=account_id, broker="zerodha"
    )
    claim = await oauth_state.consume(r, token)
    assert not oauth_state.matches(claim, account_id=account_id, broker="groww")


async def test_concurrent_redemptions_cannot_both_succeed():
    """Redemption is a DELETE that returns the value, so two callbacks racing
    on one token resolve to exactly one winner."""
    import asyncio

    r = redis()
    token = await oauth_state.issue(
        r, user_id=uuid.uuid4(), account_id=uuid.uuid4(), broker="zerodha"
    )

    async def attempt():
        try:
            return await oauth_state.consume(r, token)
        except oauth_state.StateError:
            return None

    results = await asyncio.gather(attempt(), attempt())
    assert sum(1 for x in results if x is not None) == 1


# ── the callback route itself ────────────────────────────────────────


class FakeResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeDb:
    """Returns an account only when the query would have matched one; the
    route's own filters decide that, so None models 'not yours'."""

    def __init__(self, account=None):
        self._account = account

    async def execute(self, *args, **kwargs):
        return FakeResult(self._account)

    async def commit(self):
        pass


def account_row(user_id):
    from types import SimpleNamespace

    return SimpleNamespace(
        id=uuid.uuid4(), user_id=user_id, broker="zerodha", status="pending_auth",
        status_message=None, credential_ref="ZERODHA_MAIN",
    )


async def test_callback_without_state_is_refused(monkeypatch):
    """The exact pre-fix attack: a bare account id and a request token."""
    from app.api.routes import brokers as brokers_routes

    monkeypatch.setattr(brokers_routes, "get_redis", redis)
    response = await brokers_routes.zerodha_callback(
        FakeDb(), request_token="tok", account_id=uuid.uuid4(), state=None
    )
    assert "error=invalid_state" in response.headers["location"]


async def test_callback_with_a_forged_state_is_refused(monkeypatch):
    from app.api.routes import brokers as brokers_routes

    monkeypatch.setattr(brokers_routes, "get_redis", redis)
    response = await brokers_routes.zerodha_callback(
        FakeDb(), request_token="tok", account_id=uuid.uuid4(), state="forged"
    )
    assert "error=invalid_state" in response.headers["location"]


async def test_callback_refuses_a_state_naming_a_different_account(monkeypatch):
    """A state minted for the attacker's own account, replayed with a victim's
    account id in the query string."""
    from app.api.routes import brokers as brokers_routes

    r = redis()
    monkeypatch.setattr(brokers_routes, "get_redis", lambda: r)
    token = await oauth_state.issue(
        r, user_id=uuid.uuid4(), account_id=uuid.uuid4(), broker="zerodha"
    )
    response = await brokers_routes.zerodha_callback(
        FakeDb(), request_token="tok", account_id=uuid.uuid4(), state=token
    )
    assert "error=invalid_state" in response.headers["location"]


async def test_callback_binds_only_an_account_the_state_owner_holds(monkeypatch):
    """The account lookup filters on the user id carried in the state, so a
    state stolen from one user cannot bind another user's account."""
    from app.api.routes import brokers as brokers_routes

    r = redis()
    monkeypatch.setattr(brokers_routes, "get_redis", lambda: r)
    token = await oauth_state.issue(
        r, user_id=uuid.uuid4(), account_id=uuid.uuid4(), broker="zerodha"
    )
    # No account matches that (id, user_id) pair.
    response = await brokers_routes.zerodha_callback(
        FakeDb(account=None), request_token="tok", state=token
    )
    assert "error=unknown_account" in response.headers["location"]


async def test_callback_without_a_request_token_fails_before_touching_state(monkeypatch):
    from app.api.routes import brokers as brokers_routes

    monkeypatch.setattr(brokers_routes, "get_redis", redis)
    response = await brokers_routes.zerodha_callback(FakeDb(), request_token=None, state="x")
    assert "error=zerodha_auth_failed" in response.headers["location"]
