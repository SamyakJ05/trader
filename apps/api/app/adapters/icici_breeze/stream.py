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


class BreezeTickStream:
    """Breeze ticks as an async iterator of our own Tick.

    `stock_codes` are Breeze's own codes (RELIND, not RELIANCE) — the platform
    stores each broker's native codes, so no translation happens here.
    """

    def __init__(
        self,
        *,
        user_id: str,
        session_key: str,
        stock_codes: list[str],
        exchange: Exchange = Exchange.NSE,
    ):
        if not user_id or not session_key:
            raise BrokerError("Breeze streaming needs a user id and a live session key")
        self._user_id = user_id
        self._session_key = session_key
        self._stock_codes = list(stock_codes)
        self._exchange = exchange
        self._queue: asyncio.Queue[Tick] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._client = None
        self._closed = asyncio.Event()
        self._dropped = 0

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
        # Breeze emits ticks on a channel named for the feed rather than a
        # documented event name, so the catch-all handler is what receives
        # them; the payload shape is what identifies a tick.
        client.on("*", self._on_event)
        self._client = client

        await client.connect(
            LIVE_STREAM_URL,
            auth={"user": self._user_id, "token": self._session_key},
            transports=["websocket"],
            wait_timeout=20,
        )
        logger.info("breeze_stream_started", instruments=len(self._stock_codes))

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
        for code in self._stock_codes:
            await self._client.emit(
                "join",
                {
                    "stock_code": code,
                    "exchange_code": self._exchange.value,
                    "product_type": "cash",
                    "get_exchange_quotes": True,
                    "get_market_depth": False,
                },
            )
        logger.info("breeze_stream_subscribed", instruments=len(self._stock_codes))

    async def _on_disconnect(self) -> None:
        logger.info("breeze_stream_disconnected")

    async def _on_event(self, event: str, *args) -> None:
        for payload in args:
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

        Breeze sends several message shapes over one channel and its field
        names vary by feed, so this reads defensively and drops anything it
        cannot identify rather than guessing a price.
        """
        if not isinstance(payload, dict):
            return None
        symbol = payload.get("stock_code") or payload.get("symbol")
        if not symbol:
            return None
        raw_price = (
            payload.get("last")
            if payload.get("last") is not None
            else payload.get("ltp", payload.get("last_traded_price"))
        )
        if raw_price is None:
            return None
        try:
            price = Decimal(str(raw_price))
        except ArithmeticError:
            return None
        if not price.is_finite() or price <= 0:
            return None
        return Tick(
            symbol=str(symbol),
            exchange=self._exchange,
            last_price=price,
            ts=self._timestamp(payload),
        )

    def _timestamp(self, payload) -> datetime:
        """Exchange time where Breeze gives one, arrival time otherwise.

        Breeze's timestamps are exchange-local (IST) and naive. Reading one as
        UTC would place every tick five and a half hours early — inside the
        previous session.
        """
        from app.domain.calendar import IST

        raw = payload.get("ltt") or payload.get("exchange_timestamp")
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
