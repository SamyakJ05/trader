"""Breeze live tick stream.

Breeze streams over socket.io. Unlike Kite's SDK — which brings Twisted and
needs a thread bridge — python-socketio has a native asyncio client, so this
connection lives on the same loop as the rest of the application and needs no
crossing.

Breeze also splits its streams across separate hosts: ticks on one, order
notifications on another, candles on a third. Only ticks are consumed here;
order state comes from polling, because Breeze's order stream would be a second
unverified path to the same information.

Authentication is a socketio `auth` payload of {user, token}, not the signed
headers every REST call carries. That asymmetry is Breeze's, not ours.

NONE OF THIS IS DOCUMENTED. Breeze's published API reference covers REST only
— the socket URL, the auth payload, the subscription format, the event name
and the tick layout exist only in their Python SDK's source
(breeze_connect/__init__.py, SocketEventBreeze). Everything here follows that
source. An earlier version of this file guessed instead, and guessed wrong in
three separate ways at once, each of which silently produced a connected
socket that delivered nothing:

  * subscribed by emitting a dict of stock codes, where Breeze's rooms are
    named by a token STRING, "4.1!<numeric_token>";
  * listened on a catch-all "*" for an event Breeze actually names "stock";
  * expected a dict payload, where a tick arrives as a positional list.

A feed that connects, logs a subscription count, and produces no ticks looks
exactly like a quiet market from the outside. Outside market hours the two are
indistinguishable — which is why the verification playbook insists this be
checked during a session.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal

from app.adapters.base import BrokerError
from app.core.logging import get_logger
from app.domain.enums import Exchange
from app.domain.models import Tick

logger = get_logger(__name__)

LIVE_STREAM_URL = "https://livestream.icicidirect.com"

# Same reasoning as the Kite feed: a bounded queue means memory cannot grow
# without limit behind a slow consumer, and the newest price is the one worth
# keeping when it fills.
QUEUE_SIZE = 10_000


# Breeze names its subscription rooms "<exchange>.<feed>!<token>": exchange 1
# is BSE, 4 is NSE, 13 is NFO; feed 1 is exchange quotes and 2 is market
# depth. Taken from the SDK's own EXCHANGE_MAP / get_stock_token_value.
_STREAM_EXCHANGE_PREFIX = {
    Exchange.BSE: "1",
    Exchange.NSE: "4",
    Exchange.NFO: "13",
}
_QUOTES_FEED = "1"

# The event Breeze actually emits ticks on (SDK: sio.on('stock', ...)).
_TICK_EVENT = "stock"

# A quote tick arrives as a positional list. Only the fields this platform
# consumes are named here; the SDK parses 21 of them for exchange quotes.
# Index 0 is the room name the tick came from ("4.1!2885"), which is how a
# tick is matched back to an instrument -- the payload carries no stock code.
_IX_ROOM = 0
_IX_LAST = 4
_IX_LTT = 12


class BreezeTickStream:
    """Breeze ticks as an async iterator of our own Tick.

    Takes `token_to_symbol`: Breeze's numeric security-master token mapped to
    the stock code the platform stores (2885 -> RELIND). Both halves are
    needed and neither is optional -- subscription is by token, because that
    is what names the room, and every tick comes back carrying only that room
    name, so the symbol has to be recovered from this mapping. Subscribing by
    stock code, which an earlier version did, joins a room that does not
    exist and yields silence.
    """

    def __init__(
        self,
        *,
        user_id: str,
        session_key: str,
        token_to_symbol: dict[str, str],
        exchange: Exchange = Exchange.NSE,
    ):
        if not user_id or not session_key:
            raise BrokerError("Breeze streaming needs a user id and a live session key")
        if exchange not in _STREAM_EXCHANGE_PREFIX:
            raise BrokerError(
                f"Breeze streaming does not cover {exchange.value}; "
                f"known: {', '.join(e.value for e in _STREAM_EXCHANGE_PREFIX)}"
            )
        self._user_id = user_id
        self._session_key = session_key
        self._exchange = exchange
        # room name -> stock code, built once so every tick is a dict lookup
        prefix = f"{_STREAM_EXCHANGE_PREFIX[exchange]}.{_QUOTES_FEED}!"
        self._rooms = {
            f"{prefix}{token}": symbol
            for token, symbol in token_to_symbol.items()
            if token and symbol
        }
        self._queue: asyncio.Queue[Tick] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._client = None
        self._closed = asyncio.Event()
        self._dropped = 0
        self._unknown_rooms: set[str] = set()

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        import socketio

        client = socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=0,  # keep trying; the supervisor decides when to stop
            reconnection_delay=5,
        )
        client.on("connect", self._on_connect)
        client.on("disconnect", self._on_disconnect)
        # "stock" is the event Breeze emits ticks on, per their SDK. A
        # catch-all "*" was used here before on the assumption the name was
        # undocumented and therefore unknowable; it is neither, and a
        # catch-all that never matched produced a silent feed.
        client.on(_TICK_EVENT, self._on_tick)
        self._client = client

        await client.connect(
            LIVE_STREAM_URL,
            auth={"user": self._user_id, "token": self._session_key},
            transports=["websocket"],
            wait_timeout=20,
        )
        logger.info("breeze_stream_started", instruments=len(self._rooms))

    async def stop(self) -> None:
        self._closed.set()
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # pragma: no cover - shutdown races
                logger.exception("breeze_stream_disconnect_failed")
            self._client = None
        if self._dropped:
            logger.warning("breeze_stream_dropped_ticks", dropped=self._dropped)

    async def __aenter__(self) -> "BreezeTickStream":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    # ── consumption ──────────────────────────────────────────────────

    async def ticks(self) -> AsyncIterator[Tick]:
        while not self._closed.is_set():
            try:
                yield await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                # A quiet market is not an error; loop so the closed flag is
                # still observed and a caller can cancel cleanly.
                continue

    # ── socket callbacks, all on this loop ───────────────────────────

    async def _on_connect(self) -> None:
        # Subscribing means joining a room by its name, as a bare string.
        # Emitting a descriptive dict here -- which an earlier version did --
        # is accepted by socket.io without complaint and joins nothing.
        #
        # Re-subscribing on every connect, not only the first, is deliberate:
        # reconnection is on, and room membership does not survive a drop.
        # Without this a reconnect restores the socket and loses the feed.
        for room in self._rooms:
            await self._client.emit("join", room)
        logger.info(
            "breeze_stream_subscribed",
            instruments=len(self._rooms),
            exchange=self._exchange.value,
        )

    async def _on_disconnect(self) -> None:
        logger.info("breeze_stream_disconnected")

    async def _on_tick(self, payload) -> None:
        tick = self._normalise(payload)
        if tick is not None:
            self._publish(tick)

    def _publish(self, tick: Tick) -> None:
        try:
            self._queue.put_nowait(tick)
        except asyncio.QueueFull:
            # Drop the oldest: a stale price is worth less than the current
            # one, and this runs on the loop that also serves the consumer.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(tick)
            except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                pass
            self._dropped += 1

    def _normalise(self, payload) -> Tick | None:
        """Breeze payload to our Tick, or None if it is not a usable quote.

        A quote tick is a positional LIST, not a dict: the SDK's parse_data
        maps 21 positions onto names. This reads the three it needs by index.
        The previous version required a dict and therefore dropped every tick
        that ever arrived.

        The dict branch is kept because Breeze also emits order-notification
        payloads in dict form on adjacent streams, and being handed one here
        should be ignored rather than crash.
        """
        if isinstance(payload, dict):
            # Not a quote tick. Ignored rather than guessed at.
            return None
        if not isinstance(payload, (list, tuple)) or len(payload) <= _IX_LAST:
            return None

        # The room name is the only instrument identity a tick carries.
        room = str(payload[_IX_ROOM])
        symbol = self._rooms.get(room)
        if symbol is None:
            # Ticks for a room we did not join, or a room-name format we do
            # not recognise. Logged once per room: at tick rates, logging
            # every occurrence would be its own outage.
            if room not in self._unknown_rooms:
                self._unknown_rooms.add(room)
                logger.warning("breeze_tick_unknown_room", room=room)
            return None

        try:
            price = Decimal(str(payload[_IX_LAST]))
        except (ArithmeticError, ValueError):
            return None
        if not price.is_finite() or price <= 0:
            return None
        return Tick(
            symbol=symbol,
            exchange=self._exchange,
            last_price=price,
            ts=self._timestamp(
                payload[_IX_LTT] if len(payload) > _IX_LTT else None
            ),
        )

    def _timestamp(self, raw) -> datetime:
        """Exchange time where Breeze gives one, arrival time otherwise.

        Breeze's timestamps are exchange-local (IST) and naive. Reading one as
        UTC would place every tick five and a half hours early — inside the
        previous session. Their SDK formats this with strftime('%c'), which is
        the first format tried below.
        """
        from app.domain.calendar import IST

        if isinstance(raw, datetime):
            moment = raw if raw.tzinfo else raw.replace(tzinfo=IST)
            return moment.astimezone(timezone.utc)
        if isinstance(raw, str):
            for fmt in ("%a %b %d %H:%M:%S %Y", "%Y-%m-%d %H:%M:%S"):
                try:
                    parsed = datetime.strptime(raw, fmt)
                except ValueError:
                    continue
                return parsed.replace(tzinfo=IST).astimezone(timezone.utc)
        return datetime.now(timezone.utc)
