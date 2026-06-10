"""Groww Trading API adapter.

STATUS: SCAFFOLD. Endpoint paths and payload shapes follow Groww's public
Trading API docs (https://groww.in/trade-api/docs) as of writing, but the
API is newer and shifts — every path/payload below MUST be re-verified
against current docs and a real account before any live use. Trading
methods raise until verified.

Auth model: either a daily access token generated from the dashboard, or
api_key + secret where the secret seeds a TOTP used to mint a token.
Token is sent as `Authorization: Bearer {token}`.
"""

from collections.abc import AsyncIterator
from decimal import Decimal

import httpx

from app.adapters.base import (
    BrokerAdapter,
    BrokerError,
    FeatureNotSupportedError,
    SessionExpiredError,
)
from app.core.security import decrypt_secret
from app.domain.enums import Broker, Exchange, OrderStatus, OrderSide, OrderType, ProductType
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

API_BASE = "https://api.groww.in/v1"

_STATUS_MAP = {
    "NEW": OrderStatus.SUBMITTED,
    "ACKED": OrderStatus.OPEN,
    "OPEN": OrderStatus.OPEN,
    "EXECUTED": OrderStatus.FILLED,
    "COMPLETED": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
}


class GrowwAdapter(BrokerAdapter):
    broker = Broker.GROWW

    def _token(self) -> str:
        # Preference order: env-provided daily token, then stored session token.
        if self.credentials.access_token:
            return self.credentials.access_token
        if self.account.session_token_enc:
            return decrypt_secret(self.account.session_token_enc)
        raise SessionExpiredError(
            "No Groww access token. Set <credential_ref>_ACCESS_TOKEN or complete TOTP flow."
        )

    async def connect(self) -> dict:
        if self.credentials.access_token or self.account.session_token_enc:
            await self.get_profile()
            return {"status": "connected", "flow": "token"}
        # TODO(groww-totp): implement api_key+secret -> TOTP -> access token
        # flow per https://groww.in/trade-api/docs (pyotp on the secret, then
        # token mint endpoint). Until then, paste a dashboard-generated token
        # into <credential_ref>_ACCESS_TOKEN.
        raise BrokerError(
            "Groww TOTP token-generation flow not implemented; "
            "provide <credential_ref>_ACCESS_TOKEN instead"
        )

    async def refresh_session(self) -> dict:
        await self.get_profile()
        return {"status": "connected"}

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/json",
            "X-API-VERSION": "1.0",
        }
        async with httpx.AsyncClient(base_url=API_BASE, timeout=15) as client:
            resp = await client.request(method, path, headers=headers, **kwargs)
        if resp.status_code == 401:
            raise SessionExpiredError("Groww token rejected (401)")
        body = resp.json()
        if resp.status_code >= 400:
            raise BrokerError(f"Groww error: {body}", raw=body)
        return body.get("payload", body)

    # ── account data (scaffold: paths per docs, unverified) ──────────

    async def get_profile(self) -> BrokerProfile:
        data = await self._request("GET", "/user/profile")  # TODO(verify path)
        return BrokerProfile(
            broker_client_id=str(data.get("groww_user_id", data.get("user_id", ""))),
            name=data.get("name"),
            raw=data,
        )

    async def get_funds(self) -> Funds:
        data = await self._request("GET", "/margins/detail/user")  # TODO(verify path)
        return Funds(
            available_cash=Decimal(str(data.get("clear_cash", data.get("net_margin", 0)))),
            raw=data,
        )

    async def get_holdings(self) -> list[Holding]:
        data = await self._request("GET", "/holdings/user")  # TODO(verify path)
        return [
            Holding(
                symbol=h.get("trading_symbol", h.get("symbol", "")),
                exchange=Exchange.NSE,
                quantity=int(h.get("quantity", 0)),
                average_price=Decimal(str(h.get("average_price", 0))),
            )
            for h in data.get("holdings", [])
        ]

    async def get_positions(self) -> list[BrokerPosition]:
        data = await self._request("GET", "/positions/user")  # TODO(verify path)
        return [
            BrokerPosition(
                symbol=p.get("trading_symbol", ""),
                exchange=Exchange(p.get("exchange", "NSE")),
                product=ProductType(p.get("product", "MIS")),
                quantity=int(p.get("quantity", 0)),
                average_price=Decimal(str(p.get("average_price", 0))),
            )
            for p in data.get("positions", [])
        ]

    async def get_orders(self) -> list[BrokerOrder]:
        data = await self._request("GET", "/order/list")  # TODO(verify path)
        return [
            BrokerOrder(
                broker_order_id=str(o.get("groww_order_id", "")),
                symbol=o.get("trading_symbol", ""),
                exchange=Exchange(o.get("exchange", "NSE")),
                side=OrderSide(o.get("transaction_type", "BUY")),
                order_type=OrderType(o.get("order_type", "MARKET")),
                product=ProductType(o.get("product", "MIS")),
                quantity=int(o.get("quantity", 0)),
                filled_quantity=int(o.get("filled_quantity", 0)),
                status=_STATUS_MAP.get(o.get("order_status", ""), OrderStatus.OPEN),
                raw=o,
            )
            for o in data.get("order_list", [])
        ]

    # ── trading: blocked until verified against real account ────────

    async def place_order(self, request: OrderRequest, client_order_id: str) -> PlaceOrderResult:
        # TODO(groww-live): POST /order/create with order_reference_id (our
        # idempotency hook). Blocked until request/response shapes are verified
        # against a funded account. Refusing loudly beats guessing silently.
        raise FeatureNotSupportedError(
            "Groww order placement is scaffolded but unverified — refusing to send"
        )

    async def modify_order(self, broker_order_id: str, request: OrderRequest) -> PlaceOrderResult:
        raise FeatureNotSupportedError("Groww modify_order scaffolded but unverified")

    async def cancel_order(self, broker_order_id: str) -> PlaceOrderResult:
        raise FeatureNotSupportedError("Groww cancel_order scaffolded but unverified")

    async def get_instruments(self, exchange: str | None = None) -> list[Instrument]:
        # Groww publishes an instruments CSV; TODO(verify current URL in docs).
        raise FeatureNotSupportedError("Groww instruments download not implemented yet")

    def subscribe_ticks(self, symbols: list[str]) -> AsyncIterator[Tick]:
        raise FeatureNotSupportedError("Groww live feed not implemented yet")
