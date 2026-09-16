"""Breeze live tick stream.

NONE OF BREEZE'S SOCKET PROTOCOL IS DOCUMENTED. Their published reference
covers REST only; the room-naming scheme, the event name and the tick layout
exist only in their Python SDK's source. Everything asserted here follows
that source.

This file previously held 18 passing tests written against a guessed
protocol -- dict payloads, a dict subscription, a catch-all event -- and it
pinned all three guesses as if they were the specification. Every test
passed while the feed could not have delivered a single tick. That is the
failure mode worth remembering: a test written from the same misreading as
the code confirms the misreading.

No network: the socket.io client is never connected.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.adapters.base import BrokerError
from app.adapters.icici_breeze.stream import BreezeTickStream
from app.domain.calendar import IST
from app.domain.enums import Exchange

# RELIANCE's real NSE security-master token, and the room Breeze names for it.
RELIND_TOKEN = "2885"
RELIND_ROOM = "4.1!2885"


def stream(**kw):
    defaults = dict(
        user_id="ICICI123",
        session_key="sess",
        token_to_symbol={RELIND_TOKEN: "RELIND"},
    )
    defaults.update(kw)
    return BreezeTickStream(**defaults)


def quote(room=RELIND_ROOM, last="2845.50", ltt=None, length=21):
    """A quote tick as Breeze sends it: a positional list, not a dict."""
    row = [""] * length
    row[0] = room
    row[4] = last
    if ltt is not None:
        row[12] = ltt
    return row


# ── construction ─────────────────────────────────────────────────────


def test_streaming_needs_credentials():
    """Connecting without them fails later and less clearly, after a socket is
    already opening."""
    with pytest.raises(BrokerError):
        BreezeTickStream(user_id="", session_key="s", token_to_symbol={"1": "X"})
    with pytest.raises(BrokerError):
        BreezeTickStream(user_id="u", session_key="", token_to_symbol={"1": "X"})


def test_an_unsupported_exchange_is_refused_at_construction():
    """Breeze's room prefixes cover BSE, NSE and NFO. Anything else would
    build room names that join nothing, which is silent."""
    with pytest.raises(BrokerError, match="does not cover"):
        stream(exchange=Exchange.MCX)


# ── subscription: rooms, not descriptions ────────────────────────────


async def test_subscribing_joins_a_room_named_by_token():
    """Breeze's rooms are "<exchange>.<feed>!<token>" strings. Emitting a
    descriptive dict -- which this did before -- is accepted by socket.io and
    joins nothing at all."""
    s = stream(token_to_symbol={"2885": "RELIND", "1594": "INFY"})
    emitted: list[tuple] = []

    class FakeClient:
        async def emit(self, event, data):
            emitted.append((event, data))

    s._client = FakeClient()
    await s._on_connect()

    assert emitted == [("join", "4.1!2885"), ("join", "4.1!1594")]
    assert all(isinstance(data, str) for _, data in emitted), (
        "a room name is a bare string; a dict joins nothing"
    )


async def test_the_room_prefix_follows_the_exchange():
    s = stream(token_to_symbol={"500325": "RELIANCE"}, exchange=Exchange.BSE)
    emitted = []

    class FakeClient:
        async def emit(self, event, data):
            emitted.append(data)

    s._client = FakeClient()
    await s._on_connect()
    assert emitted == ["1.1!500325"], "BSE is exchange 1, not 4"


async def test_reconnecting_resubscribes():
    """Room membership does not survive a drop, and reconnection is on. A
    one-shot subscription would restore the socket and lose the feed."""
    s = stream()
    emitted = []

    class FakeClient:
        async def emit(self, event, data):
            emitted.append(data)

    s._client = FakeClient()
    await s._on_connect()
    await s._on_connect()  # as the client does after a reconnect
    assert emitted == [RELIND_ROOM, RELIND_ROOM]


# ── payload shapes: a list, not a dict ───────────────────────────────


def test_a_quote_becomes_a_tick():
    tick = stream()._normalise(quote())
    assert tick is not None
    assert tick.symbol == "RELIND"
    assert tick.last_price == Decimal("2845.50")


def test_the_symbol_comes_from_the_room_not_the_payload():
    """A tick carries no stock code -- only the room it came from. The
    mapping given at construction is the only way back to an instrument."""
    tick = stream(token_to_symbol={"2885": "RELIND"})._normalise(quote())
    assert tick.symbol == "RELIND"


def test_a_tick_for_a_room_we_did_not_join_is_dropped():
    tick = stream()._normalise(quote(room="4.1!9999"))
    assert tick is None


def test_an_unknown_room_is_logged_once_not_per_tick():
    """At tick rates, logging every occurrence is its own outage."""
    s = stream()
    for _ in range(100):
        s._normalise(quote(room="4.1!9999"))
    assert s._unknown_rooms == {"4.1!9999"}


def test_a_dict_payload_is_ignored_rather_than_parsed():
    """Breeze emits order notifications as dicts on adjacent streams. Being
    handed one should not crash, and must not be read as a quote."""
    assert stream()._normalise({"stock_code": "RELIND", "last": "2845.50"}) is None


def test_a_short_payload_is_dropped():
    assert stream()._normalise([RELIND_ROOM, "1", "2"]) is None


@pytest.mark.parametrize("price", ["0", "-1", "", "nonsense"])
def test_an_unusable_price_is_dropped(price):
    assert stream()._normalise(quote(last=price)) is None


# ── timestamps ───────────────────────────────────────────────────────
# Breeze's timestamps are exchange-local (IST) and naive. Reading one as UTC
# puts every tick five and a half hours early -- inside the previous session.


def test_a_naive_timestamp_is_read_as_ist():
    naive = datetime(2026, 9, 15, 10, 30, 0)
    tick = stream()._normalise(quote(ltt=naive))
    assert tick.ts == naive.replace(tzinfo=IST).astimezone(timezone.utc)


def test_breezes_string_timestamp_format_is_parsed():
    """The SDK formats this with strftime('%c')."""
    tick = stream()._normalise(quote(ltt="Mon Sep 15 10:30:00 2026"))
    expected = datetime(2026, 9, 15, 10, 30, tzinfo=IST).astimezone(timezone.utc)
    assert tick.ts == expected


def test_an_unparseable_timestamp_falls_back_to_arrival():
    before = datetime.now(timezone.utc)
    tick = stream()._normalise(quote(ltt="whenever"))
    assert tick.ts >= before


def test_an_aware_timestamp_is_left_alone():
    aware = datetime(2026, 9, 15, 5, 0, tzinfo=timezone.utc)
    tick = stream()._normalise(quote(ltt=aware))
    assert tick.ts == aware


# ── the queue ────────────────────────────────────────────────────────


async def test_a_full_queue_drops_the_oldest_tick():
    """A bounded queue means memory cannot grow behind a slow consumer, and
    the newest price is the one worth keeping."""
    import asyncio

    s = stream()
    s._queue = asyncio.Queue(maxsize=2)
    for price in ("100", "200", "300"):
        s._publish(s._normalise(quote(last=price)))
    assert s._dropped == 1
    remaining = [s._queue.get_nowait().last_price for _ in range(2)]
    assert remaining == [Decimal("200"), Decimal("300")]


async def test_the_iterator_stops_when_the_stream_closes():
    s = stream()
    s._closed.set()
    assert [t async for t in s.ticks()] == []
