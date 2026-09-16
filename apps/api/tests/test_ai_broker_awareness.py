"""What the AI layer is told about the account it is trading.

Two things had gone stale once live trading was built. The analyst's system
prompt asserted "paper-only — simulated fills, virtual cash, no real money"
unconditionally, and the autonomous agent's said "a PAPER (simulated)
account". Both are false on a live account, and the agent is the one that
places orders with no human in the loop -- so a model wrongly believing its
mistakes are free is worst exactly there.

The propose_trade tool was likewise broker-blind: it allowed MARKET, MIS and
BSE on any account, every one of which Breeze refuses, so an approved
proposal became a guaranteed rejection after the user had approved a real
trade.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.ai.analyst import build_system_prompt
from app.services.ai.tools import ToolError, validate_proposal_args


def account(environment="paper", broker="paper"):
    return SimpleNamespace(environment=environment, broker=broker)


# ── what the model is told about the money ───────────────────────────


def test_a_live_account_is_named_as_live_to_the_analyst():
    """A model told it cannot move real money reasons differently about risk
    than one told it can."""
    prompt = build_system_prompt(account("live", "icici_breeze"))
    assert "LIVE" in prompt
    assert "REAL money" in prompt
    assert "simulated fills" not in prompt


def test_a_paper_account_is_still_named_as_simulated():
    prompt = build_system_prompt(account("paper", "paper"))
    assert "simulated fills" in prompt
    assert "REAL money" not in prompt


def test_the_analyst_learns_what_its_broker_refuses():
    """The model cannot know these from the tool schema, and a proposal the
    broker refuses wastes the user's approval on an order that can never
    fill."""
    prompt = build_system_prompt(account("live", "icici_breeze"))
    assert "NO market orders" in prompt
    assert "NO intraday (MIS)" in prompt
    assert "RELIND" in prompt  # its codes are not NSE tickers


def test_a_broker_without_special_rules_gets_no_note():
    prompt = build_system_prompt(account("paper", "zerodha"))
    assert "ICICI Breeze" not in prompt


# ── the autonomous agent, which has no human in the loop ─────────────


def test_the_autonomous_agent_is_told_when_it_is_live():
    from app.engines.strategy.ai_agent import _LIVE_NOTE, _PAPER_NOTE

    assert "no human approval step" in _LIVE_NOTE.lower()
    assert "REAL order" in _LIVE_NOTE
    assert "simulated" in _PAPER_NOTE


async def test_the_agent_uses_the_environment_the_runner_passes(monkeypatch):
    """Without the runner passing it, the agent always reads "paper" and
    would trade a live account believing its fills are simulated -- a silent
    failure of the safety note itself. This asserts the agent consumes the
    key, so the runner's half is exercised by the same name.
    """
    import uuid

    import fakeredis.aioredis

    from app.engines.strategy.ai_agent import AiAgentStrategy
    from app.engines.strategy.base import StrategyContext

    captured = {}

    class FakeLLM:
        provider, model = "test", "test"

        async def generate_json(self, system, prompt, schema):
            captured["system"] = system
            return {"action": "HOLD", "quantity": 0, "reason": "no edge"}

    import app.engines.strategy.ai_agent as agent_module

    async def fake_resolve(db, user_id):
        return FakeLLM()

    monkeypatch.setattr(agent_module, "resolve_llm", fake_resolve)

    async def fake_emit(*a, **kw):
        return None

    monkeypatch.setattr(agent_module.audit, "emit", fake_emit)

    class FakeDb:
        async def commit(self):
            pass

    ctx = StrategyContext(
        symbol="RELIND",
        prices=[Decimal(x) for x in range(100, 140)],
        position_quantity=0,
        params={
            "_strategy_id": str(uuid.uuid4()),
            "_user_id": str(uuid.uuid4()),
            "_environment": "live",
        },
    )
    await AiAgentStrategy().evaluate_async(
        ctx, db=FakeDb(), redis=fakeredis.aioredis.FakeRedis(decode_responses=True)
    )
    assert "LIVE" in captured["system"]
    assert "no human approval step" in captured["system"].lower()


def test_price_context_states_the_relationship_to_the_average():
    """Asking an LLM to derive a moving average from numbers in a prompt is
    asking it to do arithmetic it is unreliable at, and a wrong average
    silently becomes a wrong trade."""
    from app.engines.strategy.ai_agent import _price_context

    rising = _price_context([Decimal(x) for x in range(100, 140)])
    assert "above" in rising
    falling = _price_context([Decimal(x) for x in range(140, 100, -1)])
    assert "below" in falling


def test_price_context_survives_too_little_history():
    from app.engines.strategy.ai_agent import _price_context

    assert "not enough" in _price_context([Decimal("100")])


# ── the proposal tool ────────────────────────────────────────────────


def valid(**kw):
    base = dict(symbol="RELIND", side="BUY", quantity=1, rationale="because",
                order_type="LIMIT", limit_price=2800, product="CNC")
    base.update(kw)
    return base


@pytest.mark.parametrize("args,expected", [
    (valid(order_type="MARKET", limit_price=None), "market orders"),
    (valid(product="MIS"), "MIS"),
    (valid(exchange="BSE"), "BSE"),
])
def test_breeze_refuses_what_it_cannot_place(args, expected):
    """Refused at the tool rather than at the adapter: the error goes back
    into the model's context, so it can correct itself before a human is ever
    asked to approve anything."""
    with pytest.raises(ToolError, match=expected):
        validate_proposal_args(args, broker="icici_breeze")


def test_a_valid_breeze_proposal_passes():
    fields = validate_proposal_args(valid(), broker="icici_breeze")
    assert fields["order_type"] == "LIMIT"
    assert fields["product"] == "CNC"


def test_other_brokers_are_unaffected():
    """Kite accepts market and MIS orders; narrowing everyone to Breeze's
    limits would be its own bug."""
    fields = validate_proposal_args(
        {"symbol": "RELIANCE", "side": "BUY", "quantity": 1, "rationale": "x"},
        broker="zerodha",
    )
    assert fields["order_type"] == "MARKET"
    assert fields["product"] == "MIS"


# ── the strategy generator ───────────────────────────────────────────


def test_the_generator_is_told_breezes_constraints():
    """The prompt used to bake `"exchange": "NSE", "product": "MIS"` into both
    strategy templates. For a Breeze account that is wrong on both counts, so
    every generated strategy emitted orders the adapter refuses -- and the
    model cannot infer any of it from the schema."""
    from app.services.ai.generator import build_system_prompt

    prompt = build_system_prompt("icici_breeze", "paper")
    assert '"product": "CNC"' in prompt
    assert "no MIS" in prompt
    assert "no market orders" in prompt
    assert "RELIND" in prompt  # its codes are not NSE tickers


def test_another_broker_gets_the_ordinary_rules():
    from app.services.ai.generator import build_system_prompt

    prompt = build_system_prompt("zerodha", "paper")
    assert '"product": "MIS"' in prompt
    assert "RELIND" not in prompt


def test_the_generator_knows_when_it_is_writing_for_real_money():
    from app.services.ai.generator import build_system_prompt

    live = build_system_prompt("zerodha", "live")
    assert "REAL orders" in live
    assert "simulated" not in live
    assert "simulated" in build_system_prompt("zerodha", "paper")


def test_an_unknown_broker_still_gets_usable_rules():
    """A generator that says nothing about exchange or product produces a
    draft the strategies API then rejects."""
    from app.services.ai.generator import build_system_prompt

    prompt = build_system_prompt(None, None)
    assert "NSE" in prompt


# ── F&O proposals ────────────────────────────────────────────────────


def fo(**kw):
    base = dict(symbol="NIFTY", exchange="NFO", side="BUY", quantity=65,
                rationale="because", order_type="LIMIT", limit_price=245,
                product="NRML", expiry="2026-09-29", strike=25000, right="call")
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def test_an_option_proposal_carries_its_contract():
    """Without these the approval places a cash order in the underlying -- a
    different position, at a different price, with different margin, from the
    one the analyst described and the user approved."""
    from datetime import date

    fields = validate_proposal_args(fo(), broker="icici_breeze")
    assert fields["expiry"] == date(2026, 9, 29)
    assert fields["strike"] == Decimal("25000")
    assert fields["option_right"] == "CALL"


def test_a_futures_proposal_needs_no_strike():
    fields = validate_proposal_args(
        fo(strike=None, right=None), broker="icici_breeze"
    )
    assert fields["strike"] is None
    assert fields["option_right"] == "OTHERS"


@pytest.mark.parametrize("args,expected", [
    (fo(expiry=None), "expiry is required"),
    (fo(strike=None), "strike"),
    (fo(expiry="29-Sep-2026"), "YYYY-MM-DD"),
    (fo(right="maybe"), "call, put or others"),
])
def test_an_incoherent_contract_is_refused(args, expected):
    """Refused at the tool so the error reaches the model's context, rather
    than becoming a broker rejection after a human approved it."""
    with pytest.raises(ToolError, match=expected):
        validate_proposal_args(args, broker="icici_breeze")


def test_a_cash_proposal_carries_no_contract():
    fields = validate_proposal_args(valid(), broker="icici_breeze")
    assert fields["expiry"] is None
    assert fields["strike"] is None
    assert fields["option_right"] is None
