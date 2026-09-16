"""Zerodha Kite Connect adapter.

STATUS: SCAFFOLD. Wired against documented Kite Connect v3 REST endpoints
(https://kite.trade/docs/connect/v3/) but NOT verified against a live
account. Do not enable live trading until every method here has been
exercised with real credentials. WebSocket ticks ARE implemented, in
ticker.py and tick_feed, and are unverified in the same way.

Every order path pins variety=regular. AMO, bracket, cover and iceberg
orders are not supported here, and the capability matrix says so.

Auth model (Kite Connect v3):
1. User visits login_url -> Zerodha login -> redirect to our callback with request_token.
2. We exchange: checksum = SHA256(api_key + request_token + api_secret),
   POST /session/token -> access_token.
3. access_token expires daily; every request carries
   `Authorization: token {api_key}:{access_token}` and `X-Kite-Version: 3`.
"""

import csv
import hashlib
import io
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal

import httpx

from app.adapters.base import (
    BrokerAdapter,
    BrokerError,
    FeatureNotSupportedError,
    SessionExpiredError,
    as_int,
)
from app.adapters.throttle import kite_limiter
from app.adapters.zerodha.ticker import KiteTickFeed
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.core.security import decrypt_secret
from app.domain.enums import (
    Broker,
    Exchange,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
)
from app.domain.models import (
    BrokerOrder,
    BrokerPosition,
    BrokerProfile,
    Funds,
    Holding,
    Instrument,
    OrderRequest,
    PlaceOrderResult,
    Tick,
)

logger = get_logger(__name__)

API_BASE = "https://api.kite.trade"
LOGIN_BASE = "https://kite.zerodha.com/connect/login"

# Kite order status -> internal status. Kite has more granular states;
# anything unmapped falls back to OPEN to stay conservative.
_STATUS_MAP = {
    "OPEN": OrderStatus.OPEN,
    "TRIGGER PENDING": OrderStatus.OPEN,
    "AMO REQ RECEIVED": OrderStatus.SUBMITTED,
    "PUT ORDER REQ RECEIVED": OrderStatus.SUBMITTED,
    "VALIDATION PENDING": OrderStatus.SUBMITTED,
    "OPEN PENDING": OrderStatus.SUBMITTED,
    "COMPLETE": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
}

_ORDER_TYPE_MAP = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.SL: "SL",
    OrderType.SL_M: "SL-M",
}


class ZerodhaAdapter(BrokerAdapter):
    broker = Broker.ZERODHA

    # ── auth ─────────────────────────────────────────────────────────

    def login_url(self) -> str:
        if not self.credentials.api_key:
            raise BrokerError("Missing ZERODHA api key — set <credential_ref>_API_KEY in env")
        return f"{LOGIN_BASE}?v=3&api_key={self.credentials.api_key}"

    async def connect(self) -> dict:
        return {"login_url": self.login_url(), "flow": "redirect"}

    async def exchange_request_token(self, request_token: str) -> dict:
        """Step 2 of Kite login: trade request_token for access_token."""
        if not self.credentials.has_api_keys:
            raise BrokerError("Missing Zerodha api key/secret in environment")
        checksum = hashlib.sha256(
            (self.credentials.api_key + request_token + self.credentials.api_secret).encode()
        ).hexdigest()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{API_BASE}/session/token",
                data={
                    "api_key": self.credentials.api_key,
                    "request_token": request_token,
                    "checksum": checksum,
                },
                headers={"X-Kite-Version": "3"},
            )
        data = resp.json()
        if resp.status_code != 200:
            raise BrokerError(f"Kite session exchange failed: {data}", raw=data)
        return data["data"]  # contains access_token, user_id, etc.

    async def refresh_session(self) -> dict:
        # Kite access tokens cannot be refreshed server-side; they expire daily.
        # Validate the stored token by hitting the profile endpoint.
        try:
            await self.get_profile()
            return {"status": "connected"}
        except SessionExpiredError:
            raise
        except BrokerError as e:
            raise SessionExpiredError(f"Kite session invalid: {e}") from e

    def _auth_headers(self) -> dict:
        token_enc = self.account.session_token_enc
        if not token_enc:
            raise SessionExpiredError("No Zerodha session; complete the login flow first")
        access_token = decrypt_secret(token_enc)
        return {
            "X-Kite-Version": "3",
            "Authorization": f"token {self.credentials.api_key}:{access_token}",
        }

    def _throttle_category(self, path: str) -> str:
        """Kite allows different rates per endpoint family.

        /instruments used to share the quote bucket, which is backwards:
        quote is Kite's tightest limit at 1/s, and the instruments dump is a
        once-a-day CSV that would eat a budget the strategy loop needs every
        second. It belongs with everything else at the default rate.
        """
        if path.startswith("/quote"):
            return "quote"
        if path.startswith("/instruments/historical"):
            return "historical"
        if path.startswith("/orders"):
            return "order"
        return "default"

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        # Every Kite call passes through here, which is why the throttle sits
        # at this one point rather than at each call site. The budget is per
        # Kite app and shared across processes, so it lives in Redis: an
        # in-process limiter would be wrong by however many workers are up,
        # and exceeding the limit blocks the whole app rather than one request.
        if self.credentials.api_key:
            await kite_limiter(
                get_redis(),
                api_key=self.credentials.api_key,
                category=self._throttle_category(path),
            ).acquire()
        async with httpx.AsyncClient(base_url=API_BASE, timeout=15) as client:
            resp = await client.request(method, path, headers=self._auth_headers(), **kwargs)
        if resp.status_code == 429:
            # We throttle client-side, so a 429 means our model of the limit
            # disagrees with the broker's. Say so plainly rather than letting
            # it surface as a generic error.
            raise BrokerError(
                "Kite rate limit exceeded despite client-side throttling — "
                "the configured rate may be higher than the broker now allows"
            )
        if resp.status_code == 403:
            raise SessionExpiredError("Kite returned 403 — daily token likely expired")
        try:
            body = resp.json()
        except ValueError as exc:
            # Kite's documented 502/503/504 come from the edge as HTML, and
            # json() then raises JSONDecodeError -- not a BrokerError, so it
            # escaped every handler in the service layer as an unhandled 500
            # instead of a broker failure the account status could record.
            raise BrokerError(
                f"Kite returned a non-JSON response (HTTP {resp.status_code})",
                retryable=resp.status_code >= 500,
            ) from exc
        if body.get("status") == "error":
            # error_type is Kite's authoritative discriminator and was being
            # discarded. TokenException is documented as arriving with a 403,
            # which the branch above catches -- but only when the status code
            # survives intact. Reading the field means an expired daily token
            # is recognised as one however it arrives, so the account is
            # marked SESSION_EXPIRED and the operator is prompted to log in,
            # rather than ERROR, which reads as "the broker is broken" and
            # silently drops orders through a trading day.
            message = body.get("message", "Kite error")
            if body.get("error_type") == "TokenException":
                raise SessionExpiredError(f"Kite session expired: {message}")
            raise BrokerError(f"{message} [{body.get('error_type', 'unknown')}]", raw=body)
        return body.get("data", {})

    # ── account data ─────────────────────────────────────────────────

    async def get_profile(self) -> BrokerProfile:
        data = await self._request("GET", "/user/profile")
        return BrokerProfile(
            broker_client_id=data.get("user_id", ""),
            name=data.get("user_name"),
            email=data.get("email"),
            raw=data,
        )

    async def get_funds(self) -> Funds:
        # `net`, not `available.cash`. Kite documents available.cash as "raw
        # cash balance" -- it excludes collateral, intraday_payin and
        # adhoc_margin, and does NOT subtract utilised.debits, so it
        # under-reports for a pledged account and over-reports whenever
        # anything is deployed. `net` is the segment's "net cash balance
        # available for trading", which is the number Kite's own dashboard
        # shows as available margin. Same shape as Breeze's
        # total_bank_balance: a plausible name for the wrong figure.
        data = await self._request("GET", "/user/margins/equity")
        return Funds(
            available_cash=Decimal(str(data.get("net", 0) or 0)),
            margin_used=Decimal(str(data.get("utilised", {}).get("debits", 0) or 0)),
            raw=data,
        )

    async def get_holdings(self) -> list[Holding]:
        data = await self._request("GET", "/portfolio/holdings")
        return [
            Holding(
                symbol=h["tradingsymbol"],
                exchange=Exchange(h.get("exchange", "NSE")),
                quantity=h.get("quantity", 0),
                average_price=Decimal(str(h.get("average_price", 0))),
                last_price=Decimal(str(h.get("last_price", 0))),
                pnl=Decimal(str(h.get("pnl", 0))),
            )
            for h in data
        ]

    async def get_positions(self) -> list[BrokerPosition]:
        # `net` is the right list (carry-forward inclusive), but `realised`
        # and `unrealised` alongside it are Kite's INTRADAY figures. For a
        # position held overnight they describe today's slice, not the
        # position -- `pnl` is the overall number. Using the intraday one
        # understates P&L on anything carried, which matters if a daily-loss
        # rule ever reads broker positions.
        #
        # Enum casts are guarded: MTF is a Kite product our ProductType
        # cannot express, and one MTF position used to raise ValueError
        # inside the comprehension and hide every other position.
        data = await self._request("GET", "/portfolio/positions")
        out: list[BrokerPosition] = []
        for p in data.get("net", []):
            try:
                exchange = Exchange(str(p.get("exchange") or "NSE").upper())
                product = ProductType(str(p.get("product") or "MIS").upper())
            except ValueError:
                logger.warning(
                    "kite_position_unmappable",
                    symbol=p.get("tradingsymbol"),
                    exchange=p.get("exchange"),
                    product=p.get("product"),
                )
                continue
            out.append(
                BrokerPosition(
                    symbol=p["tradingsymbol"],
                    exchange=exchange,
                    product=product,
                    quantity=as_int(p.get("quantity")),
                    average_price=Decimal(str(p.get("average_price", 0) or 0)),
                    last_price=Decimal(str(p.get("last_price", 0) or 0)),
                    realized_pnl=Decimal(str(p.get("realised", 0) or 0)),
                    unrealized_pnl=Decimal(str(p.get("pnl", 0) or 0)),
                )
            )
        return out

    async def get_orders(self) -> list[BrokerOrder]:
        data = await self._request("GET", "/orders")
        return [self._map_order(o) for o in data]

    def _map_order(self, o: dict) -> BrokerOrder:
        # Kite's order_type list is not closed, and defaulting an unrecognised
        # one to MARKET claims a resting limit or stop order is a market order
        # -- in the platform's own model of an order that is live at the
        # broker. That is the "limit becomes market" substitution, arriving
        # through reconciliation rather than placement, and it feeds fill
        # logic and position state. LIMIT is the conservative default: it
        # describes an order that rests rather than one that executes.
        raw_type = o.get("order_type")
        order_type = next(
            (k for k, v in _ORDER_TYPE_MAP.items() if v == raw_type), None
        )
        if order_type is None:
            logger.warning(
                "kite_order_unknown_type", order_id=o.get("order_id"), order_type=raw_type
            )
            order_type = OrderType.LIMIT
        return BrokerOrder(
            broker_order_id=str(o["order_id"]),
            symbol=o["tradingsymbol"],
            exchange=Exchange(o.get("exchange", "NSE")),
            side=OrderSide(o.get("transaction_type", "BUY")),
            order_type=order_type,
            product=ProductType(o.get("product", "MIS")),
            quantity=o.get("quantity", 0),
            filled_quantity=o.get("filled_quantity", 0),
            price=Decimal(str(o["price"])) if o.get("price") else None,
            average_fill_price=(
                Decimal(str(o["average_price"])) if o.get("average_price") else None
            ),
            status=_STATUS_MAP.get(o.get("status", ""), OrderStatus.OPEN),
            status_message=o.get("status_message"),
            placed_at=(
                datetime.fromisoformat(o["order_timestamp"]) if o.get("order_timestamp") else None
            ),
            raw=o,
        )

    # ── trading ──────────────────────────────────────────────────────

    async def place_order(self, request: OrderRequest, client_order_id: str) -> PlaceOrderResult:
        # TODO(live-verification): exercised against docs only, never a real
        # account. Verify payload + error handling with a 1-share test order
        # before enabling live trading.
        payload = {
            "tradingsymbol": request.symbol,
            "exchange": request.exchange.value,
            "transaction_type": request.side.value,
            "order_type": _ORDER_TYPE_MAP[request.order_type],
            "quantity": str(request.quantity),
            "product": request.product.value,
            "validity": request.validity.value,
            # Kite echoes `tag` back on orders/postbacks — our idempotency hook.
            "tag": client_order_id[:20],
        }
        if request.price is not None:
            payload["price"] = str(request.price)
        if request.trigger_price is not None:
            payload["trigger_price"] = str(request.trigger_price)

        data = await self._request("POST", "/orders/regular", data=payload)
        # str(None) is "None", a perfectly valid-looking string. Without this
        # guard an order whose fate is unknown was recorded as SUBMITTED with
        # a poison id, and every later modify/cancel/reconcile targeted
        # /orders/regular/None.
        order_id = data.get("order_id")
        if not order_id:
            raise BrokerError("Kite accepted the order without returning an id", raw=data)
        return PlaceOrderResult(
            broker_order_id=str(order_id),
            status=OrderStatus.SUBMITTED,
            raw=data,
        )

    async def modify_order(self, broker_order_id: str, request: OrderRequest) -> PlaceOrderResult:
        payload = {
            "order_type": _ORDER_TYPE_MAP[request.order_type],
            "quantity": str(request.quantity),
            "validity": request.validity.value,
        }
        if request.price is not None:
            payload["price"] = str(request.price)
        if request.trigger_price is not None:
            payload["trigger_price"] = str(request.trigger_price)
        data = await self._request("PUT", f"/orders/regular/{broker_order_id}", data=payload)
        order_id = data.get("order_id")
        if not order_id:
            raise BrokerError("Kite accepted the modify without returning an id", raw=data)
        return PlaceOrderResult(
            broker_order_id=str(order_id), status=OrderStatus.SUBMITTED, raw=data
        )

    async def cancel_order(self, broker_order_id: str) -> PlaceOrderResult:
        data = await self._request("DELETE", f"/orders/regular/{broker_order_id}")
        return PlaceOrderResult(
            broker_order_id=str(data.get("order_id")), status=OrderStatus.CANCELLED, raw=data
        )

    # ── market data ──────────────────────────────────────────────────

    async def get_instruments(self, exchange: str | None = None) -> list[Instrument]:
        path = f"/instruments/{exchange}" if exchange else "/instruments"
        async with httpx.AsyncClient(base_url=API_BASE, timeout=60) as client:
            resp = await client.get(path, headers=self._auth_headers())
        if resp.status_code != 200:
            raise BrokerError(f"Kite instruments dump failed: HTTP {resp.status_code}")
        reader = csv.DictReader(io.StringIO(resp.text))
        out: list[Instrument] = []
        for row in reader:
            try:
                out.append(
                    Instrument(
                        symbol=row["tradingsymbol"],
                        exchange=Exchange(row["exchange"]),
                        broker_token=row.get("instrument_token"),
                        name=row.get("name"),
                        tick_size=Decimal(row["tick_size"]) if row.get("tick_size") else None,
                        lot_size=int(row["lot_size"]) if row.get("lot_size") else None,
                        instrument_type=row.get("instrument_type"),
                    )
                )
            except (ValueError, KeyError):
                continue  # skip exchanges/segments we don't model yet
        return out

    async def tick_feed(self, token_to_symbol: dict[int, str]) -> KiteTickFeed:
        """A started Kite tick feed for these instruments.

        Kite's socket speaks in numeric instrument tokens, so the caller passes
        the mapping back to trading symbols — it comes from the instruments
        sync, which is the only thing that knows both.

        Returns the feed rather than an iterator so the caller owns its
        lifetime: the underlying reactor thread must be stopped explicitly.
        """
        if not self.credentials.api_key:
            raise BrokerError("Missing Zerodha api key")
        token_enc = self.account.session_token_enc
        if not token_enc:
            raise SessionExpiredError("Kite ticks need a live session; connect first")
        access_token = decrypt_secret(token_enc)
        feed = KiteTickFeed(
            api_key=self.credentials.api_key,
            access_token=access_token,
            token_to_symbol=token_to_symbol,
        )
        await feed.start()
        return feed

    def subscribe_ticks(self, symbols: list[str]) -> AsyncIterator[Tick]:
        """Not supported through this interface.

        The base interface takes trading symbols, but Kite subscribes by
        numeric instrument token, and resolving one to the other needs the
        instruments table this adapter does not own. Use `tick_feed` with a
        token mapping instead.
        """
        raise FeatureNotSupportedError(
            "Kite subscribes by instrument token, not symbol — use tick_feed()"
        )
