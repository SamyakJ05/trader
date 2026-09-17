"""The daily research pass.

What is worth testing here is not that the analyst says anything useful --
that is the model's job and cannot be asserted. It is that the pass stays
inside its limits: it reasons only about symbols the platform actually has
data for, it tells the model the real ceilings, and a failure in it cannot
take down the ticks that move money.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.services.ai import research


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return list(self._rows)


class FakeDb:
    """Answers each select in the order research.py issues them."""

    def __init__(self, *result_sets):
        self._results = list(result_sets)

    async def execute(self, *a, **kw):
        return FakeResult(self._results.pop(0) if self._results else [])


def account(**overrides):
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        broker="icici_breeze",
        environment="live",
        auto_execute=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def rule(rule_type, params, enabled=True):
    return SimpleNamespace(rule_type=rule_type, params=params, enabled=enabled)


# ── the candidate list ───────────────────────────────────────────────


async def test_candidates_come_from_strategies_and_holdings():
    """Both, because they answer different questions: a strategy symbol is
    something to consider buying, a held one is something to consider
    closing."""
    db = FakeDb([["RELIND", "INFTEC"]], ["TATAMO"])
    assert await research.candidates(db, account()) == ["RELIND", "INFTEC", "TATAMO"]


async def test_a_symbol_in_both_appears_once():
    db = FakeDb([["RELIND"]], ["RELIND"])
    assert await research.candidates(db, account()) == ["RELIND"]


async def test_the_candidate_list_is_capped():
    """Each candidate costs tool calls against the broker's rate budget, and a
    long list would imply a breadth of market data this platform lacks."""
    many = [f"SYM{i}" for i in range(20)]
    db = FakeDb([many], [])
    assert len(await research.candidates(db, account())) == research.MAX_CANDIDATES


async def test_no_candidates_means_no_llm_call(monkeypatch):
    """An account with no strategies and no holdings has no candles, so there
    is nothing to reason about. Calling the model anyway would spend tokens to
    be told so."""
    called = []

    async def must_not_resolve(*a, **kw):  # pragma: no cover
        called.append(1)
        raise AssertionError("no provider should be resolved with no candidates")

    monkeypatch.setattr(research, "resolve_llm", must_not_resolve)
    result = await research.run_daily_research(FakeDb([], []), None, account())
    assert result == {"status": "no_candidates", "proposals": []}
    assert called == []


# ── what the model is told ───────────────────────────────────────────


def test_the_prompt_states_the_real_limits():
    note = research._limits_note(
        [
            rule("MAX_DAILY_TURNOVER", {"max_turnover": 10000}),
            rule("MAX_DAILY_LOSS", {"max_loss": 500}),
            rule("MAX_ORDER_NOTIONAL", {"max_notional": 2500}),
        ]
    )
    assert "10000" in note and "500" in note and "2500" in note


def test_a_disabled_rule_is_not_quoted_as_a_limit():
    """Quoting a limit that is switched off would have the model sizing
    against a ceiling nothing enforces."""
    note = research._limits_note(
        [rule("MAX_DAILY_TURNOVER", {"max_turnover": 10000}, enabled=False)]
    )
    assert "10000" not in note


def test_having_no_limits_is_said_rather_than_left_blank():
    assert "no limits are configured" in research._limits_note([])


def test_the_prompt_does_not_promise_a_daily_trade():
    """A model asked for a trade every day will find a reason for one. The
    prompt has to say that doing nothing is usually right, or the scheduling
    itself becomes the argument for trading."""
    assert "Doing nothing is the right answer on most days" in research.PROMPT
    assert "propose only what you would defend" in research.PROMPT


def test_the_prompt_admits_the_candidate_list_is_not_a_screen():
    """The honest constraint: these are the only symbols with data, not the
    best ones found. Implying otherwise would be a claim about data nobody
    has."""
    assert "cannot see the wider market" in research.PROMPT
    assert "not imply the list was screened" in research.PROMPT


# ── failure containment ──────────────────────────────────────────────


async def test_one_account_failing_does_not_stop_the_others(monkeypatch):
    accounts = [account(), account(), account()]
    seen = []

    async def sometimes_explode(db, redis, acct):
        seen.append(acct.id)
        if acct.id == accounts[0].id:
            raise RuntimeError("provider rate limit")
        return {"status": "ok", "proposals": []}

    monkeypatch.setattr(research, "run_daily_research", sometimes_explode)

    class Db(FakeDb):
        async def execute(self, *a, **kw):
            return FakeResult(accounts)

    results = await research.research_all_accounts(Db(), None)
    assert len(seen) == 3, "a failure must not stop the remaining accounts"
    assert [r["status"] for r in results] == ["failed", "ok", "ok"]


async def test_research_never_raises_out_of_the_worker(monkeypatch):
    """It shares a worker with the ticks that fill orders and reconcile them.
    An advisory pass must not be able to mark that worker unhealthy."""

    class Db(FakeDb):
        async def execute(self, *a, **kw):
            raise RuntimeError("database went away")

    async def boom(db, redis, acct):  # pragma: no cover
        raise AssertionError("not reached")

    monkeypatch.setattr(research, "run_daily_research", boom)
    with pytest.raises(RuntimeError):
        # research_all_accounts guards per account, not the initial listing --
        # the worker job is what swallows this, and that is asserted below.
        await research.research_all_accounts(Db(), None)


async def test_the_worker_job_swallows_everything(monkeypatch):
    from app.workers import jobs

    async def explode(*a, **kw):
        raise RuntimeError("provider outage")

    monkeypatch.setattr(jobs.research, "research_all_accounts", explode)
    # Must return, not raise: the same worker runs order reconciliation.
    await jobs.ai_research_tick({})
