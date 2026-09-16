"""Autonomous AI trading: the analyst placing orders with no human approval.

What matters here is what auto-execute does NOT remove. It takes the person
out of the loop and nothing else: the risk engine, the kill switches, the
live gates and the idempotency key all still apply, and an account that has
not opted in is unaffected. The tests are written around those boundaries
rather than the happy path, because the happy path is the part that is
obvious when it breaks.
"""

import uuid
from types import SimpleNamespace

import pytest
from fakeredis import FakeAsyncRedis

from app.domain.enums import (
    AIProposalStatus,
    Environment,
    Exchange,
    OrderSide,
    OrderType,
    ProductType,
    RiskRuleType,
)
from app.domain.models import OrderRequest
from app.engines.risk.engine import RiskEngine
from app.services.ai import analyst, tools


@pytest.fixture
def redis():
    return FakeAsyncRedis(decode_responses=True)


def account(auto_execute=False, environment="paper", broker="paper"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        environment=environment,
        broker=broker,
        auto_execute=auto_execute,
    )


# ── what the model is told ───────────────────────────────────────────
#
# A model that believes a human will review its proposal, on an account where
# nobody will, is reasoning about the wrong situation. These assert the
# instructions match the account's actual behaviour.


def test_a_manual_account_tells_the_analyst_approval_is_required():
    described = next(
        t for t in tools.tools_for(account(auto_execute=False)) if t["name"] == "propose_trade"
    )
    assert "must approve" in described["description"] or "approval" in described["description"]
    assert "AUTOMATIC" not in described["description"]


def test_an_automatic_account_tells_the_analyst_no_one_will_review():
    described = next(
        t for t in tools.tools_for(account(auto_execute=True)) if t["name"] == "propose_trade"
    )
    assert "AUTOMATIC" in described["description"]
    assert "no human review" in described["description"].lower()


def test_describing_one_account_does_not_mutate_the_shared_tool_list():
    """tools_for copies before editing; a leak here would put one account's
    instructions on every later request in the process."""
    before = next(t for t in tools.TOOLS if t["name"] == "propose_trade")["description"]
    tools.tools_for(account(auto_execute=True))
    after = next(t for t in tools.TOOLS if t["name"] == "propose_trade")["description"]
    assert before == after
    assert "AUTOMATIC" not in after


def test_the_system_prompt_warns_when_nobody_is_watching():
    prompt = analyst.build_system_prompt(account(auto_execute=True))
    assert "AUTOMATIC" in prompt
    assert "NO human review" in prompt


def test_the_system_prompt_is_unchanged_for_a_manual_account():
    prompt = analyst.build_system_prompt(account(auto_execute=False))
    assert "AUTOMATIC mode" not in prompt


# ── the daily cap ────────────────────────────────────────────────────


class CountingDb:
    """Returns a fixed count for the auto-trade query."""

    def __init__(self, count):
        self._count = count

    def add(self, obj):  # pragma: no cover - not exercised by _check_rule
        pass

    async def execute(self, *a, **kw):
        count = self._count

        class Result:
            def scalar_one(inner):
                return count

        return Result()


def order_request(quantity=1):
    return OrderRequest(
        symbol="RELIANCE",
        exchange=Exchange.NSE,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        product=ProductType.CNC,
        quantity=quantity,
        price=100,
    )


async def check_cap(count, *, auto_executed, limit=10, redis=None):
    engine = RiskEngine(CountingDb(count), redis)
    return await engine._check_rule(
        RiskRuleType.MAX_AUTO_TRADES_PER_DAY,
        {"max_auto_trades": limit},
        uuid.uuid4(),
        account(),
        order_request(),
        Environment.PAPER.value,
        None,
        auto_executed,
    )


async def test_an_automatic_trade_under_the_cap_passes(redis):
    assert await check_cap(3, auto_executed=True, redis=redis) is None


async def test_an_automatic_trade_at_the_cap_is_refused(redis):
    reason = await check_cap(10, auto_executed=True, redis=redis)
    assert reason is not None
    assert "10 of 10" in reason


async def test_a_human_approved_trade_is_never_capped(redis):
    """The cap substitutes for human judgement. Someone clicking approve has
    already applied it, so rate-limiting them would be the wrong limit."""
    assert await check_cap(999, auto_executed=False, redis=redis) is None


# ── the account gate ─────────────────────────────────────────────────


async def test_propose_trade_on_a_manual_account_never_places_an_order(monkeypatch, redis):
    """The default path must be untouched: a proposal, and nothing else."""
    placed = []

    async def fail_if_called(*a, **kw):  # pragma: no cover - asserted not called
        placed.append(kw)
        raise AssertionError("a manual account must not place an order")

    monkeypatch.setattr(tools.execute, "place_from_proposal", fail_if_called)

    recorded = {}

    class Db:
        def add(self, obj):
            recorded["proposal"] = obj

        async def flush(self):
            recorded["proposal"].id = uuid.uuid4()

        async def commit(self):
            recorded["committed"] = True

        async def execute(self, *a, **kw):  # pragma: no cover
            raise AssertionError("no query expected")

    monkeypatch.setattr(tools.audit, "emit", _noop_emit)

    import json

    result = json.loads(
        await tools.run_tool(
            Db(),
            redis,
            uuid.uuid4(),
            account(auto_execute=False),
            "propose_trade",
            _proposal_args(),
        )
    )
    assert result["status"] == AIProposalStatus.PROPOSED.value
    assert placed == []


async def test_propose_trade_on_an_automatic_account_places_the_order(monkeypatch, redis):
    calls = {}

    async def fake_place(db, proposal, acct, *, auto_executed):
        calls["auto_executed"] = auto_executed
        calls["proposal"] = proposal
        return SimpleNamespace(
            id=uuid.uuid4(), status="ACCEPTED", status_message=None
        )

    monkeypatch.setattr(tools.execute, "place_from_proposal", fake_place)
    monkeypatch.setattr(tools.audit, "emit", _noop_emit)

    import json

    result = json.loads(
        await tools.run_tool(
            _RecordingDb(),
            redis,
            uuid.uuid4(),
            account(auto_execute=True),
            "propose_trade",
            _proposal_args(),
        )
    )
    assert result["status"] == AIProposalStatus.AUTO_EXECUTED.value
    assert result["order_status"] == "ACCEPTED"
    # Marked as unattended, which is what the daily cap counts and what the
    # order history reports.
    assert calls["auto_executed"] is True


async def test_a_refused_automatic_trade_is_recorded_not_dropped(monkeypatch, redis):
    """A proposal that failed to execute must leave a trace. Returning the
    failure to the model rather than raising also lets it respond to it."""

    async def refuse(db, proposal, acct, *, auto_executed):
        raise tools.execute.ProposalNotExecutable("Breeze does not accept MARKET orders")

    monkeypatch.setattr(tools.execute, "place_from_proposal", refuse)
    monkeypatch.setattr(tools.audit, "emit", _noop_emit)

    import json

    db = _RecordingDb()
    result = json.loads(
        await tools.run_tool(
            db, redis, uuid.uuid4(), account(auto_execute=True), "propose_trade", _proposal_args()
        )
    )
    assert result["status"] == AIProposalStatus.AUTO_FAILED.value
    assert "MARKET" in result["error"]
    assert db.proposal.status == AIProposalStatus.AUTO_FAILED.value


async def test_a_risk_rejection_is_reported_as_such_not_as_placed(monkeypatch, redis):
    """A risk block is an order row in REJECTED_RISK, not an exception. The
    model must not be told 'placed' when the order was refused."""

    async def rejected(db, proposal, acct, *, auto_executed):
        return SimpleNamespace(
            id=uuid.uuid4(),
            status="REJECTED_RISK",
            status_message="Order notional 500000.00 exceeds limit 25000.00",
        )

    monkeypatch.setattr(tools.execute, "place_from_proposal", rejected)
    monkeypatch.setattr(tools.audit, "emit", _noop_emit)

    import json

    result = json.loads(
        await tools.run_tool(
            _RecordingDb(),
            redis,
            uuid.uuid4(),
            account(auto_execute=True),
            "propose_trade",
            _proposal_args(),
        )
    )
    assert result["order_status"] == "REJECTED_RISK"
    assert "exceeds limit" in result["order_message"]


# ── helpers ──────────────────────────────────────────────────────────


async def _noop_emit(*a, **kw):
    return None


def _proposal_args():
    return {
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "side": "BUY",
        "order_type": "LIMIT",
        "product": "CNC",
        "quantity": 1,
        "limit_price": "100",
        "rationale": "test",
    }


class _RecordingDb:
    def __init__(self):
        self.proposal = None

    def add(self, obj):
        self.proposal = obj

    async def flush(self):
        self.proposal.id = uuid.uuid4()

    async def commit(self):
        return None

    async def execute(self, *a, **kw):  # pragma: no cover
        raise AssertionError("no query expected")
