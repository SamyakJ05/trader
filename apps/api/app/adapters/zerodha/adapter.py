"""Zerodha Kite Connect adapter.

STATUS: SCAFFOLD. Wired against documented Kite Connect v3 REST endpoints
(https://kite.trade/docs/connect/v3/) but NOT verified against a live
account. Do not enable live trading until every method here has been
exercised with real credentials. WebSocket ticks are not implemented.

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
)
from app.adapters.throttle import kite_limiter
from app.adapters.zerodha.ticker import KiteTickFeed
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
        """Kite allows different rates per endpoint family."""
        if path.startswith("/quote") or path.startswith("/instruments"):
            return "quote"
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
        body = resp.json()
        if body.get("status") == "error":
            raise BrokerError(body.get("message", "Kite error"), raw=body)
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
        data = await self._request("GET", "/user/margins/equity")
        return Funds(
            available_cash=Decimal(str(data.get("available", {}).get("cash", 0))),
            margin_used=Decimal(str(data.get("utilised", {}).get("debits", 0))),
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
        data = await self._request("GET", "/portfolio/positions")
        return [
            BrokerPosition(
                symbol=p["tradingsymbol"],
                exchange=Exchange(p.get("exchange", "NSE")),
                product=ProductType(p.get("product", "MIS")),
                quantity=p.get("quantity", 0),
                average_price=Decimal(str(p.get("average_price", 0))),
                last_price=Decimal(str(p.get("last_price", 0))),
                realized_pnl=Decimal(str(p.get("realised", 0))),
                unrealized_pnl=Decimal(str(p.get("unrealised", 0))),
            )
            for p in data.get("net", [])
        ]

    async def get_orders(self) -> list[BrokerOrder]:
        data = await self._request("GET", "/orders")
        return [self._map_order(o) for o in data]

    def _map_order(self, o: dict) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id=str(o["order_id"]),
            symbol=o["tradingsymbol"],
            exchange=Exchange(o.get("exchange", "NSE")),
            side=OrderSide(o.get("transaction_type", "BUY")),
            order_type=next(
                (k for k, v in _ORDER_TYPE_MAP.items() if v == o.get("order_type")),
                OrderType.MARKET,
            ),
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
        return PlaceOrderResult(
            broker_order_id=str(data.get("order_id")),
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
        return PlaceOrderResult(
            broker_order_id=str(data.get("order_id")), status=OrderStatus.SUBMITTED, raw=data
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
