"""Cross-user isolation tests.

The platform is multi-tenant: every user has their own broker accounts,
orders, positions, strategies and risk rules. A missing user_id filter lets
one user read or act on another user's data — including placing orders
against someone else's broker account.

Each test here is an attacker-shaped test: user A attempts an action against
user B's resource and must be refused. Pure-unit (fakeredis, no DB), matching
the rest of the suite.
"""

import uuid
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from fastapi import HTTPException

from app.services import killswitch
from app.services.orders import OrderServiceError, place_order


def user(is_admin: bool = False):
    return SimpleNamespace(id=uuid.uuid4(), is_admin=is_admin, is_active=True)


def account_row(user_id, environment="paper", broker="paper"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        broker=broker,
        environment=environment,
        live_enabled=False,
        label="Main",
    )


def strategy_row(user_id, status="RUNNING"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        status=status,
        name="victim strategy",
    )


class FakeResult:
    """Mimics the subset of SQLAlchemy Result the routes use."""

    def __init__(self, value=None, rows=()):
        self._value = value
        self._rows = rows

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return iter(self._rows)


class FakeDb:
    """Records what was asked for; returns None for ownership-filtered
    lookups so 'not yours' is indistinguishable from 'does not exist'."""

    def __init__(self, get_returns=None, execute_returns=None):
        self._get_returns = get_returns
        self._execute_returns = execute_returns
        self.added = []
        self.committed = False

    async def get(self, model, pk):
        return self._get_returns

    async def execute(self, *args, **kwargs):
        return FakeResult(self._execute_returns)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass


# ── place_order: the choke point must enforce its own invariant ──────


async def test_place_order_rejects_account_owned_by_another_user():
    """services/orders.py claims 'account ownership check' as step 1 of the
    pipeline. It must actually perform it, not trust the caller — this is the
    backstop that closes every current and future call site at once."""
    attacker = user()
    victim = user()
    victims_account = account_row(victim.id)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    with pytest.raises(OrderServiceError) as exc:
        await place_order(
            FakeDb(),
            redis,
            user_id=attacker.id,
            account=victims_account,
            request=SimpleNamespace(symbol="RELIANCE"),
            client_order_id="attack-001",
        )
    assert "does not belong" in str(exc.value).lower() or "ownership" in str(exc.value).lower()


async def test_place_order_allows_own_account_past_the_ownership_check():
    """The guard must not break the legitimate path. The stub db cannot carry
    an order all the way through, so we assert the specific thing that
    matters: the ownership guard did not fire for a matching account."""
    owner = user()
    own_account = account_row(owner.id)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    with pytest.raises(Exception) as exc:
        await place_order(
            FakeDb(),
            redis,
            user_id=owner.id,
            account=own_account,
            request=SimpleNamespace(symbol="RELIANCE"),
            client_order_id="legit-001",
        )
    # The stub cannot carry an order to completion, so it fails somewhere
    # downstream. The assertion that matters is which failure it is NOT:
    # an ownership refusal would surface as OrderServiceError.
    assert not isinstance(exc.value, OrderServiceError)


# ── strategy creation must validate the broker account ───────────────


async def test_create_strategy_rejects_another_users_broker_account():
    """A strategy row bound to a foreign broker_account_id is loaded verbatim
    by the runner, which then places orders through the victim's account."""
    from app.api.routes.strategies import StrategyBody, create_strategy

    attacker = user()
    victims_account_id = uuid.uuid4()
    body = StrategyBody(
        name="pivot",
        kind="sma_crossover",
        broker_account_id=victims_account_id,
        symbols=["RELIANCE"],
    )
    db = FakeDb(execute_returns=None)  # ownership-filtered lookup finds nothing

    with pytest.raises(HTTPException) as exc:
        await create_strategy(body, attacker, db)
    assert exc.value.status_code == 404
    assert db.added == [], "no strategy row may be created for a foreign account"


# ── per-strategy kill switch must check ownership BEFORE writing ─────


async def test_strategy_killswitch_rejects_another_users_strategy(monkeypatch):
    """Engaging a kill switch on a foreign strategy is denial-of-trading:
    it halts the victim's strategy on every runner tick and flips their row
    to KILLED. The ownership check must happen before the Redis key is set."""
    from app.api.routes import system as system_routes

    attacker = user()
    victims_strategy_id = uuid.uuid4()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(system_routes, "get_redis", lambda: redis)

    body = system_routes.KillSwitchBody(
        scope="strategy", engaged=True, strategy_id=victims_strategy_id
    )
    db = FakeDb(execute_returns=None)  # not owned by attacker

    with pytest.raises(HTTPException) as exc:
        await system_routes.set_killswitch(body, attacker, db)
    assert exc.value.status_code == 404

    key = killswitch.STRATEGY_KEY.format(strategy_id=victims_strategy_id)
    assert await redis.exists(key) == 0, "Redis key written for a foreign strategy"


# ── killswitch.status must not leak other tenants' strategy IDs ──────


async def test_killswitch_status_only_returns_callers_strategies():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    mine = uuid.uuid4()
    theirs = uuid.uuid4()
    await redis.set(killswitch.STRATEGY_KEY.format(strategy_id=mine), "engaged")
    await redis.set(killswitch.STRATEGY_KEY.format(strategy_id=theirs), "engaged")

    result = await killswitch.status(redis, owned_strategy_ids={mine})

    assert str(mine) in result["killed_strategies"]
    assert str(theirs) not in result["killed_strategies"], "leaked another tenant's strategy id"


async def test_killswitch_status_with_no_owned_ids_returns_none():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await redis.set(killswitch.STRATEGY_KEY.format(strategy_id=uuid.uuid4()), "engaged")

    result = await killswitch.status(redis, owned_strategy_ids=set())

    assert result["killed_strategies"] == []


# ── global kill switch is an operator action, not a user action ──────


async def test_global_killswitch_refused_for_non_admin(monkeypatch):
    """The global switch halts strategy execution for EVERY user. It must not
    be reachable by an ordinary authenticated user."""
    from app.api.routes import system as system_routes

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(system_routes, "get_redis", lambda: redis)
    body = system_routes.KillSwitchBody(scope="global", engaged=True, reason="oops")

    with pytest.raises(HTTPException) as exc:
        await system_routes.set_killswitch(body, user(is_admin=False), FakeDb())
    assert exc.value.status_code == 403
    assert await redis.exists(killswitch.GLOBAL_KEY) == 0


async def test_global_killswitch_allowed_for_admin(monkeypatch):
    from app.api.routes import system as system_routes

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(system_routes, "get_redis", lambda: redis)
    body = system_routes.KillSwitchBody(scope="global", engaged=True, reason="maintenance")

    await system_routes.set_killswitch(body, user(is_admin=True), FakeDb())

    assert await redis.exists(killswitch.GLOBAL_KEY) == 1


# ── AI proposal approval must re-verify the broker account ───────────


async def test_approve_proposal_refuses_foreign_broker_account(monkeypatch):
    """The only order-path call site that trusted a stored FK instead of
    re-verifying ownership."""
    from app.api.routes import ai as ai_routes

    attacker = user()
    proposal = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=attacker.id,
        broker_account_id=uuid.uuid4(),  # points at someone else's account
        status="PROPOSED",
        symbol="RELIANCE",
        exchange="NSE",
        side="BUY",
        order_type="MARKET",
        product="MIS",
        quantity=1,
        limit_price=None,
    )
    # proposal lookup succeeds, ownership-filtered account lookup does not
    db = FakeDb(get_returns=proposal, execute_returns=None)

    with pytest.raises(HTTPException) as exc:
        await ai_routes.approve_proposal(proposal.id, attacker, db)
    # 409 specifically: the proposal itself IS the attacker's (so _owned_proposal
    # passes); the refusal must come from the account re-verification.
    assert exc.value.status_code == 409


# ── AI analyst chat must not accept a foreign broker account ─────────


async def test_analyst_chat_refuses_foreign_broker_account(monkeypatch):
    """The account resolved here drives every read tool (positions, orders,
    funds) and propose_trade. It must be ownership-filtered, not merely
    compared after an unscoped load."""
    from app.api.routes import ai as ai_routes

    attacker = user()
    monkeypatch.setattr(
        ai_routes, "_require_llm", lambda db, uid: _async_return(object())
    )
    body = ai_routes.ChatBody(
        broker_account_id=uuid.uuid4(),
        messages=[ai_routes.ChatMessage(role="user", content="show me positions")],
    )
    db = FakeDb(execute_returns=None)  # ownership-filtered lookup finds nothing

    with pytest.raises(HTTPException) as exc:
        await ai_routes.analyst_chat(body, attacker, db)
    assert exc.value.status_code == 404


def _async_return(value):
    async def _inner():
        return value

    return _inner()


# ── cancel/modify carry the same backstop as place_order ─────────────


async def test_cancel_order_rejects_another_users_order():
    from app.services.orders import cancel_order

    attacker = user()
    victim = user()
    victims_order = SimpleNamespace(
        id=uuid.uuid4(), user_id=victim.id, broker_account_id=uuid.uuid4(), status="OPEN"
    )
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    with pytest.raises(OrderServiceError) as exc:
        await cancel_order(FakeDb(), redis, user_id=attacker.id, order=victims_order)
    assert "does not belong" in str(exc.value).lower()


async def test_modify_order_rejects_another_users_order():
    from app.services.orders import modify_order

    attacker = user()
    victim = user()
    victims_order = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=victim.id,
        broker_account_id=uuid.uuid4(),
        status="OPEN",
        environment="paper",
        filled_quantity=0,
        quantity=10,
        price=None,
    )
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    with pytest.raises(OrderServiceError) as exc:
        await modify_order(
            FakeDb(), redis, user_id=attacker.id, order=victims_order, price=None, quantity=5
        )
    assert "does not belong" in str(exc.value).lower()
