"""Reference prices for risk checks.

Every order is valued before it is allowed. The paper simulator invents a seed
price for any symbol it has not seen, which is correct for a simulation and
dangerous for a live order: a notional check that passes against a fabricated
number is not a check at all.
"""

from decimal import Decimal
from types import SimpleNamespace

import fakeredis.aioredis
import pytest

from app.services import quotes


def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def account(environment="paper"):
    return SimpleNamespace(id="acct", environment=environment, broker="zerodha")


class FakeDb:
    """No candles stored, so the candle fallback finds nothing."""

    async def execute(self, *args, **kwargs):
        return SimpleNamespace(scalar_one_or_none=lambda: None)


# ── paper keeps the simulator ────────────────────────────────────────


async def test_a_paper_order_is_priced_by_the_simulator():
    """Inventing a price is the simulator's job and paper's whole point."""
    price = await quotes.reference_price(
        FakeDb(), redis(), account=account("paper"), symbol="NEVERSEEN"
    )
    assert price > 0


# ── live refuses to guess ────────────────────────────────────────────


async def test_a_live_order_without_a_quote_is_refused():
    """The core safety property: no real price, no order. Falling back to the
    simulator would value a real trade against a number with no relationship
    to the market."""
    with pytest.raises(quotes.NoQuoteAvailable):
        await quotes.reference_price(
            FakeDb(), redis(), account=account("live"), symbol="RELIANCE"
        )


async def test_a_live_order_uses_a_cached_real_tick():
    r = redis()
    await quotes.record_live_tick(
        r, symbol="RELIANCE", exchange="NSE", price=Decimal("2845.50")
    )
    price = await quotes.reference_price(
        FakeDb(), r, account=account("live"), symbol="RELIANCE"
    )
    assert price == Decimal("2845.50")


async def test_a_live_quote_expires_rather_than_going_stale():
    """Stored with a TTL, so an expired quote is indistinguishable from an
    absent one and cannot be read by a caller that forgot to check its age."""
    r = redis()
    await quotes.record_live_tick(
        r, symbol="RELIANCE", exchange="NSE", price=Decimal("2845.50")
    )
    ttl = await r.ttl("quote:live:NSE:RELIANCE")
    assert 0 < ttl <= quotes.MAX_QUOTE_AGE_SECONDS


async def test_quotes_are_per_symbol_and_exchange():
    """A price for one instrument must never answer for another."""
    r = redis()
    await quotes.record_live_tick(
        r, symbol="RELIANCE", exchange="NSE", price=Decimal("2845.50")
    )
    with pytest.raises(quotes.NoQuoteAvailable):
        await quotes.reference_price(
            FakeDb(), r, account=account("live"), symbol="INFY"
        )


async def test_a_corrupt_cached_quote_is_ignored():
    """Better to refuse the order than to price it off a garbage value."""
    r = redis()
    await r.set("quote:live:NSE:RELIANCE", "not-a-number")
    with pytest.raises(quotes.NoQuoteAvailable):
        await quotes.reference_price(
            FakeDb(), r, account=account("live"), symbol="RELIANCE"
        )


@pytest.mark.parametrize("bad", ["0", "-10"])
async def test_a_non_positive_cached_quote_is_ignored(bad):
    r = redis()
    await r.set("quote:live:NSE:RELIANCE", bad)
    with pytest.raises(quotes.NoQuoteAvailable):
        await quotes.reference_price(
            FakeDb(), r, account=account("live"), symbol="RELIANCE"
        )


async def test_live_price_returns_none_rather_than_inventing():
    """The accessor itself never fabricates; only the simulator does."""
    assert await quotes.live_price(FakeDb(), redis(), symbol="ANYTHING") is None


# ── the AI assistant's quote tool ────────────────────────────────────


async def test_the_ai_tool_refuses_to_invent_a_live_quote(monkeypatch):
    """The assistant reasons aloud from whatever it is given. A simulated
    price on a live account would become the stated justification for a trade
    the user then approves, with nothing marking it as fiction."""
    import json

    from app.services.ai import tools

    payload = await tools.run_tool(
        FakeDb(), redis(), "u", account("live"), "get_quotes", {"symbols": ["RELIANCE"]}
    )
    parsed = json.loads(payload)["RELIANCE"]
    assert parsed["last_price"] is None
    assert "do not" in parsed["note"].lower()


async def test_the_ai_tool_uses_a_real_live_quote_when_there_is_one():
    import json

    from app.services.ai import tools

    r = redis()
    await quotes.record_live_tick(
        r, symbol="RELIANCE", exchange="NSE", price=Decimal("2845.50")
    )
    payload = await tools.run_tool(
        FakeDb(), r, "u", account("live"), "get_quotes", {"symbols": ["RELIANCE"]}
    )
    parsed = json.loads(payload)["RELIANCE"]
    assert parsed["last_price"] == "2845.50"
    assert parsed["source"] == "market"


async def test_the_ai_tool_still_uses_the_simulator_on_paper():
    """Paper is a simulation; inventing prices is the point."""
    import json

    from app.services.ai import tools

    payload = await tools.run_tool(
        FakeDb(), redis(), "u", account("paper"), "get_quotes", {"symbols": ["ANYTHING"]}
    )
    parsed = json.loads(payload)["ANYTHING"]
    assert parsed["last_price"] is not None
    assert parsed["source"] == "simulator"
