"""Kite WebSocket tick feed.

Zerodha streams ticks in a binary protocol. Rather than parse it ourselves we
use their own SDK, which owns the packet format and follows it when it changes.

The cost is that the SDK is built on Twisted: `KiteTicker` runs a reactor and
delivers ticks through synchronous callbacks on its own thread. This module is
the containment boundary. The reactor runs threaded with signal handlers
disabled — so it never installs handlers over the ones the API process uses —
and every callback hands its payload to the asyncio loop through
`call_soon_threadsafe`. Nothing outside this file sees a Twisted object, a
thread, or an SDK type: callers get an async iterator of our own `Tick`.

Keeping that boundary here is what preserves the rule that only adapters know
anything about a broker.
"""

import asyncio
import threading
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal

from app.adapters.base import BrokerError
from app.core.logging import get_logger
from app.domain.enums import Exchange
from app.domain.models import Tick

logger = get_logger(__name__)

# Ticks arrive faster than a slow consumer drains them. A bounded queue means
# memory cannot grow without limit; when it fills we drop the oldest tick,
# because in a price feed the newest one is the one that matters.
QUEUE_SIZE = 10_000

# Kite's modes, cheapest first. "quote" carries last price plus OHLC and volume
# without the full market depth, which is what the platform actually consumes.
MODE_LTP = "ltp"
MODE_QUOTE = "quote"
MODE_FULL = "full"


class KiteTickFeed:
    """Bridges KiteTicker's threaded callbacks into an async iterator.

    One feed per broker account. `token_to_symbol` maps Kite's numeric
    instrument tokens back to the trading symbols the rest of the platform
    uses — the socket speaks only in tokens.
    """

    def __init__(
        self,
        *,
        api_key: str,
        access_token: str,
        token_to_symbol: dict[int, str],
        exchange: Exchange = Exchange.NSE,
        mode: str = MODE_QUOTE,
    ):
        if not api_key or not access_token:
            raise BrokerError("Kite ticker needs an api key and a live access token")
        self._api_key = api_key
        self._access_token = access_token
        self._token_to_symbol = dict(token_to_symbol)
        self._exchange = exchange
        self._mode = mode
        self._queue: asyncio.Queue[Tick] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ticker = None
        self._closed = threading.Event()
        self._dropped = 0

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        from kiteconnect import KiteTicker

        self._loop = asyncio.get_running_loop()
        ticker = KiteTicker(self._api_key, self._access_token)
        ticker.on_ticks = self._on_ticks
        ticker.on_connect = self._on_connect
        ticker.on_close = self._on_close
        ticker.on_error = self._on_error
        ticker.on_reconnect = self._on_reconnect
        ticker.on_noreconnect = self._on_noreconnect
        self._ticker = ticker
        # threaded=True runs the reactor on its own thread and, critically,
        # passes installSignalHandlers=False — the API process owns its signal
        # handling and a library reactor must not take it over.
        ticker.connect(threaded=True)
        logger.info(
            "kite_ticker_starting",
            instruments=len(self._token_to_symbol),
            mode=self._mode,
        )

    async def stop(self) -> None:
        self._closed.set()
        if self._ticker is not None:
            # stop_retry first: otherwise the SDK treats our close as a dropped
            # connection and reconnects the socket we are trying to shut down.
            self._ticker.stop_retry()
            self._ticker.close()
            self._ticker = None
        if self._dropped:
            logger.warning("kite_ticker_dropped_ticks", dropped=self._dropped)

    async def __aenter__(self) -> "KiteTickFeed":
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
                # still checked and the caller can cancel cleanly.
                continue

    # ── SDK callbacks, all on the reactor thread ─────────────────────

    def _publish(self, tick: Tick) -> None:
        """Hand a tick to the asyncio loop from the reactor thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        def _put() -> None:
            try:
                self._queue.put_nowait(tick)
            except asyncio.QueueFull:
                # Drop the oldest rather than the newest: a stale price is
                # worth less than the current one, and blocking here would
                # stall the reactor thread.
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(tick)
                except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                    pass
                self._dropped += 1

        loop.call_soon_threadsafe(_put)

    def _on_ticks(self, ws, ticks) -> None:
        for raw in ticks:
            tick = self._normalise(raw)
            if tick is not None:
                self._publish(tick)

    def _normalise(self, raw: dict) -> Tick | None:
        """SDK dict -> our Tick. Unknown tokens and unusable prices are
        dropped rather than guessed at."""
        token = raw.get("instrument_token")
        symbol = self._token_to_symbol.get(token)
        if symbol is None:
            return None
        price = raw.get("last_price")
        if price is None:
            return None
        try:
            last_price = Decimal(str(price))
        except (ArithmeticError, ValueError):
            return None
        if not last_price.is_finite() or last_price <= 0:
            return None
        # The SDK gives exchange timestamps as naive IST datetimes; anything
        # missing falls back to arrival time.
        ts = raw.get("exchange_timestamp") or raw.get("last_trade_time")
        if isinstance(ts, datetime):
            from app.domain.calendar import IST

            ts = ts.replace(tzinfo=IST) if ts.tzinfo is None else ts
        else:
            ts = datetime.now(timezone.utc)
        return Tick(
            symbol=symbol,
            exchange=self._exchange,
            last_price=last_price,
            ts=ts.astimezone(timezone.utc),
        )

    def _on_connect(self, ws, response) -> None:
        tokens = list(self._token_to_symbol)
        if not tokens:
            return
        ws.subscribe(tokens)
        ws.set_mode(self._mode, tokens)
        logger.info("kite_ticker_connected", instruments=len(tokens))

    def _on_close(self, ws, code, reason) -> None:
        logger.info("kite_ticker_closed", code=code, reason=str(reason))

    def _on_error(self, ws, code, reason) -> None:
        logger.warning("kite_ticker_error", code=code, reason=str(reason))

    def _on_reconnect(self, ws, attempts) -> None:
        logger.warning("kite_ticker_reconnecting", attempt=attempts)

    def _on_noreconnect(self, ws) -> None:
        # The SDK has given up. Surface it loudly: the platform is now running
        # on no live prices, which must not look like a quiet market.
        logger.error("kite_ticker_gave_up_reconnecting")
        self._closed.set()
