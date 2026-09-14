"""Kite tick feed: normalisation and the thread-to-asyncio bridge.

The SDK delivers ticks on a Twisted reactor thread. Everything here exercises
the boundary where those callbacks cross into the asyncio world, because that
crossing is where the bugs in this design live — not in the protocol, which
Zerodha's own code handles.

No network: KiteTicker itself is never started.
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.adapters.base import BrokerError
from app.adapters.zerodha.ticker import KiteTickFeed
from app.domain.calendar import IST
from app.domain.enums import Exchange

TOKENS = {738561: "RELIANCE", 408065: "INFY"}


def feed(**kwargs):
    defaults = dict(api_key="key", access_token="token", token_to_symbol=TOKENS)
    defaults.update(kwargs)
    return KiteTickFeed(**defaults)


# ── construction ─────────────────────────────────────────────────────


def test_a_feed_needs_credentials():
    """A ticker without a live access token would fail on connect with a less
    obvious error, after a reactor thread had already started."""
    with pytest.raises(BrokerError):
        KiteTickFeed(api_key="", access_token="t", token_to_symbol=TOKENS)
    with pytest.raises(BrokerError):
        KiteTickFeed(api_key="k", access_token="", token_to_symbol=TOKENS)


# ── normalisation ────────────────────────────────────────────────────


def test_a_tick_is_normalised_to_our_own_model():
    tick = feed()._normalise(
        {"instrument_token": 738561, "last_price": 2845.5,
         "exchange_timestamp": datetime(2026, 9, 15, 10, 30)}
    )
    assert tick.symbol == "RELIANCE"
    assert tick.last_price == Decimal("2845.5")
    assert tick.exchange == Exchange.NSE


def test_exchange_timestamps_are_read_as_ist():
    """The SDK hands back naive datetimes in exchange local time. Treating
    them as UTC would place every tick five and a half hours early — inside
    the previous session."""
    tick = feed()._normalise(
        {"instrument_token": 738561, "last_price": 100,
         "exchange_timestamp": datetime(2026, 9, 15, 10, 30)}
    )
    assert tick.ts == datetime(2026, 9, 15, 10, 30, tzinfo=IST).astimezone(timezone.utc)


def test_an_aware_timestamp_is_left_alone():
    aware = datetime(2026, 9, 15, 5, 0, tzinfo=timezone.utc)
    tick = feed()._normalise(
        {"instrument_token": 738561, "last_price": 100, "exchange_timestamp": aware}
    )
    assert tick.ts == aware


def test_a_tick_without_a_timestamp_falls_back_to_arrival_time():
    tick = feed()._normalise({"instrument_token": 738561, "last_price": 100})
    assert tick.ts.tzinfo is not None


def test_an_unknown_instrument_token_is_dropped():
    """The socket speaks in tokens. One we did not subscribe to, or a stale
    mapping, must not be guessed into a symbol."""
    assert feed()._normalise({"instrument_token": 999999, "last_price": 100}) is None


@pytest.mark.parametrize("price", [None, 0, -5, float("nan"), float("inf")])
def test_an_unusable_price_is_dropped(price):
    """A zero, negative or non-finite price would poison every downstream
    calculation: candles, marks, risk checks."""
    raw = {"instrument_token": 738561}
    if price is not None:
        raw["last_price"] = price
    assert feed()._normalise(raw) is None


def test_last_trade_time_is_used_when_exchange_timestamp_is_absent():
    tick = feed()._normalise(
        {"instrument_token": 738561, "last_price": 100,
         "last_trade_time": datetime(2026, 9, 15, 11, 0)}
    )
    assert tick.ts == datetime(2026, 9, 15, 11, 0, tzinfo=IST).astimezone(timezone.utc)


# ── the thread bridge ────────────────────────────────────────────────


async def test_a_tick_published_from_another_thread_reaches_the_consumer():
    """The actual bridge: callbacks fire on the reactor thread and must arrive
    on the asyncio loop without the consumer knowing about threads."""
    import threading

    f = feed()
    f._loop = asyncio.get_running_loop()

    def reactor_thread():
        f._on_ticks(None, [{"instrument_token": 738561, "last_price": 2500}])

    thread = threading.Thread(target=reactor_thread)
    thread.start()
    thread.join()

    tick = await asyncio.wait_for(f._queue.get(), timeout=2)
    assert tick.symbol == "RELIANCE"
    assert tick.last_price == Decimal("2500")


async def test_publishing_after_the_loop_is_gone_is_survivable():
    """On shutdown the reactor thread can outlive the loop briefly. Publishing
    into a dead loop must not raise on a thread nobody is watching."""
    f = feed()
    f._loop = None
    f._on_ticks(None, [{"instrument_token": 738561, "last_price": 100}])  # must not raise


async def test_a_full_queue_drops_the_oldest_tick_not_the_newest():
    """A slow consumer must not stall the reactor or grow memory without
    limit, and the newest price is the one worth keeping."""
    f = feed()
    f._loop = asyncio.get_running_loop()
    f._queue = asyncio.Queue(maxsize=2)

    for price in (100, 101, 102):
        f._on_ticks(None, [{"instrument_token": 738561, "last_price": price}])
    await asyncio.sleep(0)  # let the scheduled puts run

    prices = [(await f._queue.get()).last_price for _ in range(2)]
    assert prices == [Decimal("101"), Decimal("102")], "oldest should have been dropped"
    assert f._dropped == 1


async def test_the_iterator_stops_when_the_feed_closes():
    """Closing must end the loop rather than leaving a consumer waiting on a
    queue nothing will ever fill again."""
    f = feed()
    f._loop = asyncio.get_running_loop()
    f._closed.set()
    assert [tick async for tick in f.ticks()] == []


async def test_giving_up_on_reconnection_closes_the_feed():
    """Running on no live prices must not look like a quiet market."""
    f = feed()
    f._on_noreconnect(None)
    assert f._closed.is_set()


# ── subscription ─────────────────────────────────────────────────────


def test_connecting_subscribes_every_mapped_token():
    class FakeSocket:
        def __init__(self):
            self.subscribed = None
            self.mode = None

        def subscribe(self, tokens):
            self.subscribed = tokens

        def set_mode(self, mode, tokens):
            self.mode = (mode, tokens)

    socket = FakeSocket()
    feed()._on_connect(socket, {})
    assert sorted(socket.subscribed) == sorted(TOKENS)
    assert socket.mode[0] == "quote"


def test_connecting_with_no_instruments_subscribes_to_nothing():
    """Kite rejects an empty subscribe; there is simply nothing to ask for."""

    class FakeSocket:
        def __init__(self):
            self.called = False

        def subscribe(self, tokens):
            self.called = True

        def set_mode(self, mode, tokens):
            self.called = True

    socket = FakeSocket()
    feed(token_to_symbol={})._on_connect(socket, {})
    assert not socket.called
