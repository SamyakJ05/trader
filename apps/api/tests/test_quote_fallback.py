"""The broker REST quote, which is the third and last price source.

Found by trying to place a real order by hand: the tick stream only
subscribes to symbols a strategy names, so a symbol nobody is trading has no
cached price even with the market open and the session healthy, and the order
was refused for want of one. The websocket remains primary; this is what
answers when it has nothing to say.

The ordering is the point. A cached tick beats a candle close beats a REST
call, because that is descending freshness, and the REST call costs one
request against a tight per-minute budget.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fakeredis import FakeAsyncRedis

from app.services import quotes


@pytest.fixture
def redis():
    return FakeAsyncRedis(decode_responses=True)


def account():
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        broker="icici_breeze",
        environment="live",
        credential_ref="BREEZE_MAIN",
    )


def _adapter_returning(price, calls=None):
    class Adapter:
        async def get_quote(self, symbol, exchange="NSE"):
            if calls is not None:
                calls.append((symbol, exchange))
            return price

    return Adapter()


def _patch_adapter(monkeypatch, adapter):
    import app.adapters.registry as registry

    monkeypatch.setattr(registry, "get_adapter", lambda account: adapter)


async def test_a_cached_tick_wins_and_costs_no_api_call(monkeypatch, redis):
    """The websocket stays primary: a fresh tick must not trigger a REST call
    against the broker's per-minute budget."""
    calls: list = []
    _patch_adapter(monkeypatch, _adapter_returning(Decimal("999"), calls))
    monkeypatch.setattr(quotes.candle_store, "last_close", _returns(None))
    await quotes.record_live_tick(
        redis, symbol="RELIND", exchange="NSE", price=Decimal("1244.50")
    )

    price = await quotes.live_price(
        None, redis, symbol="RELIND", exchange="NSE", account=account()
    )
    assert price == Decimal("1244.50")
    assert calls == [], "a cached tick must not cost a broker call"


async def test_a_recent_candle_beats_the_rest_quote(monkeypatch, redis):
    calls: list = []
    _patch_adapter(monkeypatch, _adapter_returning(Decimal("999"), calls))
    monkeypatch.setattr(quotes.candle_store, "last_close", _returns(Decimal("1240")))

    price = await quotes.live_price(
        None, redis, symbol="RELIND", exchange="NSE", account=account()
    )
    assert price == Decimal("1240")
    assert calls == []


async def test_the_rest_quote_answers_when_nothing_is_cached(monkeypatch, redis):
    """The case that blocked a real order: no strategy names RELIND, so the
    stream never subscribed and there was no tick to price against."""
    calls: list = []
    _patch_adapter(monkeypatch, _adapter_returning(Decimal("1244.75"), calls))
    monkeypatch.setattr(quotes.candle_store, "last_close", _returns(None))

    price = await quotes.live_price(
        None, redis, symbol="RELIND", exchange="NSE", account=account()
    )
    assert price == Decimal("1244.75")
    assert calls == [("RELIND", "NSE")]


async def test_a_fetched_quote_is_cached_for_the_next_order(monkeypatch, redis):
    """A burst of orders in one symbol must not spend a call each."""
    calls: list = []
    _patch_adapter(monkeypatch, _adapter_returning(Decimal("1244.75"), calls))
    monkeypatch.setattr(quotes.candle_store, "last_close", _returns(None))

    for _ in range(3):
        await quotes.live_price(
            None, redis, symbol="RELIND", exchange="NSE", account=account()
        )
    assert len(calls) == 1, f"expected one broker call, got {len(calls)}"


async def test_a_cached_rest_quote_still_expires(monkeypatch, redis):
    """Caching the REST quote must not smuggle a stale price past the
    staleness rule: it is written with the same TTL as a tick, so an expired
    one is indistinguishable from absent rather than readable as current."""
    _patch_adapter(monkeypatch, _adapter_returning(Decimal("1244.75")))
    monkeypatch.setattr(quotes.candle_store, "last_close", _returns(None))
    await quotes.live_price(
        None, redis, symbol="RELIND", exchange="NSE", account=account()
    )
    ttl = await redis.ttl(quotes._live_key("RELIND", "NSE"))
    assert 0 < ttl <= quotes.MAX_QUOTE_AGE_SECONDS


async def test_no_account_means_no_rest_call(monkeypatch, redis):
    """live_price is also called where no account is in hand. That path must
    keep working and must not reach for a broker."""
    monkeypatch.setattr(quotes.candle_store, "last_close", _returns(None))
    assert (
        await quotes.live_price(None, redis, symbol="RELIND", exchange="NSE") is None
    )


async def test_a_broker_failure_is_no_quote_not_an_exception(monkeypatch, redis):
    """The caller refuses the order for want of a price either way. A broker
    being slow or rejecting the session must not surface as something else."""

    class Exploding:
        async def get_quote(self, symbol, exchange="NSE"):
            raise RuntimeError("Breeze session rejected (401)")

    _patch_adapter(monkeypatch, Exploding())
    monkeypatch.setattr(quotes.candle_store, "last_close", _returns(None))

    price = await quotes.live_price(
        None, redis, symbol="RELIND", exchange="NSE", account=account()
    )
    assert price is None


async def test_an_adapter_without_get_quote_simply_has_no_third_source(
    monkeypatch, redis
):
    _patch_adapter(monkeypatch, SimpleNamespace())
    monkeypatch.setattr(quotes.candle_store, "last_close", _returns(None))

    price = await quotes.live_price(
        None, redis, symbol="RELIND", exchange="NSE", account=account()
    )
    assert price is None


async def test_a_live_order_still_refuses_when_no_source_has_a_price(
    monkeypatch, redis
):
    """The safety property this whole module exists for: no price, no order."""
    _patch_adapter(monkeypatch, _adapter_returning(None))
    monkeypatch.setattr(quotes.candle_store, "last_close", _returns(None))

    with pytest.raises(quotes.NoQuoteAvailable):
        await quotes.reference_price(
            None, redis, account=account(), symbol="RELIND", exchange="NSE"
        )


def _returns(value):
    async def _inner(*a, **kw):
        return value

    return _inner
