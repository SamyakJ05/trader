"""Live tick ingestion.

The stream is what makes live trading possible at all: it feeds the quote
cache that risk checks read and the candles strategies read. These tests cover
the supervision and the refusals — the parts that decide whether one account's
problem becomes everyone's.
"""

import asyncio
import uuid
from decimal import Decimal
from types import SimpleNamespace

import fakeredis.aioredis
import pytest

from app.domain.enums import Exchange
from app.domain.models import Tick
from app.workers import tick_stream


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return iter(self._rows)


class FakeDb:
    def __init__(self, rows=(), account=None):
        self._rows = rows
        self._account = account

    async def execute(self, *args, **kwargs):
        return FakeResult(self._rows)

    async def get(self, model, pk):
        return self._account

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def account(status="connected"):
    return SimpleNamespace(
        id=uuid.uuid4(), broker="zerodha", environment="live", status=status,
        user_id=uuid.uuid4(),
    )


# ── which symbols to subscribe ───────────────────────────────────────


async def test_only_symbols_the_account_trades_are_subscribed():
    """Subscribing to everything would burn the broker's instrument quota and
    fill the database with candles nobody reads."""
    db = FakeDb(rows=[["RELIANCE", "INFY"], ["INFY", "TCS"]])
    assert await tick_stream._symbols_for(db, account()) == ["INFY", "RELIANCE", "TCS"]


async def test_an_account_with_no_strategies_subscribes_to_nothing():
    assert await tick_stream._symbols_for(FakeDb(rows=[]), account()) == []


async def test_a_strategy_with_no_symbols_is_survivable():
    assert await tick_stream._symbols_for(FakeDb(rows=[None, []]), account()) == []


# ── refusals ─────────────────────────────────────────────────────────


async def test_a_disconnected_account_does_not_open_a_socket(monkeypatch):
    called = False

    def fake_adapter(acct):
        nonlocal called
        called = True

    monkeypatch.setattr(tick_stream, "get_adapter", fake_adapter)
    monkeypatch.setattr(
        tick_stream, "async_session_factory",
        lambda: FakeDb(account=account(status="session_expired")),
    )
    await tick_stream._run_once(uuid.uuid4())
    assert not called


async def test_no_instrument_tokens_means_no_socket(monkeypatch):
    """Without the instrument master there is nothing to subscribe to.
    Opening a socket that receives nothing would look like a quiet market."""
    called = False

    def fake_adapter(acct):
        nonlocal called
        called = True

    monkeypatch.setattr(tick_stream, "get_adapter", fake_adapter)
    monkeypatch.setattr(
        tick_stream, "async_session_factory",
        lambda: FakeDb(rows=[["RELIANCE"]], account=account()),
    )

    async def no_tokens(db, **kwargs):
        return {}

    monkeypatch.setattr(tick_stream.instrument_service, "token_map", no_tokens)
    await tick_stream._run_once(uuid.uuid4())
    assert not called


# ── supervision ──────────────────────────────────────────────────────


async def test_an_expired_session_waits_and_reconnects_rather_than_dying(monkeypatch):
    """An expired session is a pause, not the end of the stream.

    This previously asserted the opposite -- that the task returns after one
    expiry -- on the reasoning that there is nothing to retry until the user
    logs in again. That much is true, but returning ends the task for good,
    and the supervisor only starts a task when there isn't one. So after the
    user re-authenticated the next morning, the feed stayed dead until the
    whole worker was restarted: a silent daily loss of live prices, and with
    it every live strategy, since the runner refuses to trade without a fresh
    quote.

    The concern behind the original -- hammering the broker for hours -- is
    handled by SESSION_RETRY_DELAY_SECONDS instead, which is what this
    asserts: it keeps trying, slowly, and picks the session back up.
    """
    from app.adapters.base import SessionExpiredError

    attempts = 0

    async def expired_then_cancelled(account_id):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise SessionExpiredError("token dead")
        # Stands in for the user logging back in.
        raise asyncio.CancelledError

    monkeypatch.setattr(tick_stream, "_run_once", expired_then_cancelled)
    monkeypatch.setattr(tick_stream, "SESSION_RETRY_DELAY_SECONDS", 0)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tick_stream.stream_account(uuid.uuid4()), timeout=2)
    assert attempts == 3


async def test_an_expired_session_backs_off_rather_than_spinning(monkeypatch):
    """The retry must be slow: nothing can succeed until a human logs in
    through the broker, so a tight loop would burn database reads for hours
    to learn the same thing each time."""
    assert tick_stream.SESSION_RETRY_DELAY_SECONDS >= 60
    assert tick_stream.SESSION_RETRY_DELAY_SECONDS > tick_stream.RECONNECT_DELAY_SECONDS


async def test_a_transient_failure_is_retried(monkeypatch):
    """A blip should not cost a session of prices."""
    attempts = 0

    async def fails_then_stops(account_id):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("socket dropped")
        raise asyncio.CancelledError

    monkeypatch.setattr(tick_stream, "_run_once", fails_then_stops)
    monkeypatch.setattr(tick_stream, "RECONNECT_DELAY_SECONDS", 0)
    try:
        await asyncio.wait_for(tick_stream.stream_account(uuid.uuid4()), timeout=2)
    except asyncio.CancelledError:
        pass
    assert attempts == 3


async def test_a_new_strategy_symbol_reopens_the_feed(monkeypatch):
    """A feed subscribes once, at the moment it opens.

    Without this check, a strategy created afterwards never receives a price:
    the socket stays open on the old subscription set, and the runner then
    refuses to trade the new strategy live because it has no fresh quote. The
    strategy sits silently dead and nothing says why. Returning drops back
    into stream_account's loop, which reopens with the current set.
    """
    import fakeredis.aioredis as fake_aioredis

    ticks_seen = 0

    class FakeFeed:
        async def ticks(self):
            nonlocal ticks_seen
            for _ in range(50):
                ticks_seen += 1
                yield Tick(
                    symbol="RELIANCE", exchange=Exchange.NSE,
                    last_price=Decimal("2800"),
                    ts=__import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    ),
                )

        async def stop(self):
            pass

    calls = {"n": 0}

    class ChangingDb(FakeDb):
        """Reports an extra symbol only after the feed is already open.

        The first read is the one _run_once subscribes from; every later read
        is the periodic recheck, which must see the strategy that was added
        in between.
        """

        async def execute(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResult([["RELIANCE"]])
            return FakeResult([["RELIANCE", "INFY"]])

    acct = account()
    monkeypatch.setattr(
        tick_stream, "async_session_factory",
        lambda: ChangingDb(rows=[["RELIANCE"]], account=acct),
    )

    async def tokens(db, **kwargs):
        return {"RELIANCE": "2885"}

    monkeypatch.setattr(tick_stream.instrument_service, "token_map", tokens)

    class FakeAdapter:
        async def tick_feed(self, token_map):
            return FakeFeed()

    monkeypatch.setattr(tick_stream, "get_adapter", lambda a: FakeAdapter())
    monkeypatch.setattr(
        tick_stream, "get_redis",
        lambda: fake_aioredis.FakeRedis(decode_responses=True),
    )

    # Candle writing is not what this test is about; the real one needs a
    # database.
    async def no_candle(db, tick, source):
        return None

    monkeypatch.setattr(tick_stream, "record_tick", no_candle)
    # Force the recheck on the first tick rather than waiting a minute.
    monkeypatch.setattr(tick_stream, "SYMBOL_RECHECK_SECONDS", 0)

    await tick_stream._run_once(acct.id)

    # It returned early rather than consuming all 50 ticks.
    assert ticks_seen < 50


# ── what a tick writes ───────────────────────────────────────────────


async def test_a_tick_populates_the_quote_cache():
    """This is what stops a live order being risk-checked against a price
    nobody stands behind."""
    from app.services import quotes

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    tick = Tick(
        symbol="RELIANCE", exchange=Exchange.NSE,
        last_price=Decimal("2845.50"),
        ts=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    await quotes.record_live_tick(
        redis, symbol=tick.symbol, exchange=tick.exchange.value, price=tick.last_price
    )
    assert await quotes.live_price(FakeDb(), redis, symbol="RELIANCE") == Decimal("2845.50")
