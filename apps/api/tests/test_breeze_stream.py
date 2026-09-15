"""Breeze live tick stream.

Breeze streams over socket.io with a native asyncio client, so unlike the Kite
feed there is no thread bridge to get wrong. What is easy to get wrong here is
the payload: Breeze sends several message shapes over one channel, its field
names vary by feed, and its timestamps are naive exchange-local time.

No network: the socket.io client is never connected.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.adapters.base import BrokerError
from app.adapters.icici_breeze.stream import BreezeTickStream
from app.domain.calendar import IST
from app.domain.enums import Exchange


def stream(**kw):
    defaults = dict(user_id="ICICI123", session_key="sess", stock_codes=["RELIND"])
    defaults.update(kw)
    return BreezeTickStream(**defaults)


# ── construction ─────────────────────────────────────────────────────


def test_streaming_needs_credentials():
    """Connecting without them fails later and less clearly, after a socket is
    already opening."""
    with pytest.raises(BrokerError):
        BreezeTickStream(user_id="", session_key="s", stock_codes=["X"])
    with pytest.raises(BrokerError):
        BreezeTickStream(user_id="u", session_key="", stock_codes=["X"])


# ── payload shapes ───────────────────────────────────────────────────


def test_a_quote_becomes_a_tick():
    tick = stream()._normalise({"stock_code": "RELIND", "last": "2845.50"})
    assert tick.symbol == "RELIND"
    assert tick.last_price == Decimal("2845.50")
    assert tick.exchange == Exchange.NSE


def test_the_symbol_stays_breezes_own_code():
    """The platform stores each broker's native codes, so nothing is
    translated here — RELIND is what Breeze sends and what we keep."""
    assert stream()._normalise({"stock_code": "RELIND", "last": "1"}).symbol == "RELIND"


@pytest.mark.parametrize("field", ["last", "ltp", "last_traded_price"])
def test_the_price_is_read_from_any_of_breezes_names(field):
    """Breeze's field names vary by feed; a tick whose price we cannot find
    would be dropped silently."""
    tick = stream()._normalise({"stock_code": "RELIND", field: "100.25"})
    assert tick.last_price == Decimal("100.25")


def test_a_message_that_is_not_a_quote_is_ignored():
    """Several message shapes share one channel."""
    assert stream()._normalise({"some": "other message"}) is None
    assert stream()._normalise("a string") is None
    assert stream()._normalise(None) is None


@pytest.mark.parametrize("price", [None, 0, -5, "not-a-number"])
def test_an_unusable_price_is_dropped(price):
    """One bad price would poison candles, marks and every risk check that
    reads them."""
    payload = {"stock_code": "RELIND"}
    if price is not None:
        payload["last"] = price
    assert stream()._normalise(payload) is None


# ── timestamps ───────────────────────────────────────────────────────


def test_a_naive_timestamp_is_read_as_ist():
    """Breeze's timestamps are exchange-local and naive. Reading one as UTC
    would place every tick five and a half hours early — inside the previous
    session."""
    tick = stream()._normalise(
        {"stock_code": "RELIND", "last": "100", "ltt": datetime(2026, 9, 15, 10, 30)}
    )
    assert tick.ts == datetime(2026, 9, 15, 10, 30, tzinfo=IST).astimezone(timezone.utc)


def test_breezes_string_timestamp_format_is_parsed():
    """Breeze sends times as strings like 'Tue Sep 15 10:30:00 2026'."""
    tick = stream()._normalise(
        {"stock_code": "RELIND", "last": "100", "ltt": "Tue Sep 15 10:30:00 2026"}
    )
    assert tick.ts == datetime(2026, 9, 15, 10, 30, tzinfo=IST).astimezone(timezone.utc)


def test_an_unparseable_timestamp_falls_back_to_arrival():
    """Better a slightly late timestamp than no tick at all."""
    tick = stream()._normalise(
        {"stock_code": "RELIND", "last": "100", "ltt": "whenever"}
    )
    assert tick.ts.tzinfo is not None


def test_an_aware_timestamp_is_left_alone():
    aware = datetime(2026, 9, 15, 5, 0, tzinfo=timezone.utc)
    tick = stream()._normalise({"stock_code": "RELIND", "last": "100", "ltt": aware})
    assert tick.ts == aware


# ── backpressure ─────────────────────────────────────────────────────


async def test_a_full_queue_drops_the_oldest_tick():
    """A slow consumer must not grow memory without limit, and the newest
    price is the one worth keeping."""
    import asyncio

    s = stream()
    s._queue = asyncio.Queue(maxsize=2)
    for price in ("100", "101", "102"):
        s._publish(s._normalise({"stock_code": "RELIND", "last": price}))

    prices = [(await s._queue.get()).last_price for _ in range(2)]
    assert prices == [Decimal("101"), Decimal("102")]
    assert s._dropped == 1


async def test_the_iterator_stops_when_the_stream_closes():
    s = stream()
    s._closed.set()
    assert [tick async for tick in s.ticks()] == []


# ── subscription ─────────────────────────────────────────────────────


async def test_connecting_subscribes_every_stock_code():
    emitted = []

    class FakeClient:
        async def emit(self, event, payload):
            emitted.append((event, payload))

    s = stream(stock_codes=["RELIND", "INFTEC"])
    s._client = FakeClient()
    await s._on_connect()

    assert {payload["stock_code"] for _, payload in emitted} == {"RELIND", "INFTEC"}
    assert all(event == "join" for event, _ in emitted)
