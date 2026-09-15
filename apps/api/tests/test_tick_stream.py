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


async def test_an_expired_session_stops_retrying(monkeypatch):
    """Nothing to retry until the user logs in again; looping would hammer the
    broker for hours."""
    from app.adapters.base import SessionExpiredError

    attempts = 0

    async def always_expired(account_id):
        nonlocal attempts
        attempts += 1
        raise SessionExpiredError("token dead")

    monkeypatch.setattr(tick_stream, "_run_once", always_expired)
    await asyncio.wait_for(tick_stream.stream_account(uuid.uuid4()), timeout=2)
    assert attempts == 1


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
