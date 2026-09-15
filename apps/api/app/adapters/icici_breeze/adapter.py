"""ICICI Direct Breeze adapter.

STATUS: SCAFFOLD. Request-signing (checksum) wiring follows Breeze docs
(https://api.icicidirect.com/breezeapi/documents/index.html) but nothing
here has been exercised against a live account. Trading methods refuse
until verified.

Auth model:
1. User logs in at https://api.icicidirect.com/apiuser/login?api_key=...
   -> redirect carries an `apisession` value (Breeze calls it session key).
2. We exchange it via /customerdetails for a session_token.
3. Every request is signed: checksum = SHA256(timestamp + json_body + secret_key),
   headers X-Checksum: "token {checksum}", X-Timestamp, X-AppKey, X-SessionToken.

Documented limits (verify current numbers): 100 calls/min, 5000/day per user.
Both are enforced client-side in _request, per credential ref, because Breeze
documents its limits per user and exceeding them blocks the account rather
than returning a retryable error.
"""

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import quote_plus

import httpx

from app.adapters.base import (
    BrokerAdapter,
    BrokerError,
    FeatureNotSupportedError,
    SessionExpiredError,
)
from app.adapters.throttle import DailyQuotaExceeded, breeze_limiter, breeze_quota
from app.core.logging import get_logger
from app.core.redis import get_redis
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

logger = get_logger(__name__)

API_BASE = "https://api.icicidirect.com/breezeapi/api/v1"
LOGIN_BASE = "https://api.icicidirect.com/apiuser/login"

_STATUS_MAP = {
    "Ordered": OrderStatus.OPEN,
    "Requested": OrderStatus.SUBMITTED,
    "Executed": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELLED,
    "Rejected": OrderStatus.REJECTED,
    "Partially Executed": OrderStatus.PARTIALLY_FILLED,
}

_PRODUCT_MAP = {ProductType.CNC: "cash", ProductType.MIS: "margin", ProductType.NRML: "futures"}


class BreezeAdapter(BrokerAdapter):
    broker = Broker.ICICI_BREEZE

    def login_url(self) -> str:
        if not self.credentials.api_key:
            raise BrokerError("Missing Breeze api key — set <credential_ref>_API_KEY in env")
        return f"{LOGIN_BASE}?api_key={quote_plus(self.credentials.api_key)}"

    async def connect(self) -> dict:
        return {"login_url": self.login_url(), "flow": "redirect"}

    async def exchange_session(self, api_session: str) -> dict:
        """Exchange the apisession value from the redirect for a session token."""
        async with httpx.AsyncClient(timeout=15) as client:
            # Breeze uses GET-with-body; httpx's .get() helper rejects json,
            # so build the request explicitly.
            resp = await client.request(
                "GET",
                f"{API_BASE}/customerdetails",
                json={"SessionToken": api_session, "AppKey": self.credentials.api_key},
            )
        data = resp.json()
        if resp.status_code != 200 or not data.get("Success"):
            raise BrokerError(f"Breeze session exchange failed: {data}", raw=data)
        return data["Success"]  # contains session_token

    async def refresh_session(self) -> dict:
        try:
            await self.get_funds()
            return {"status": "connected"}
        except BrokerError as e:
            raise SessionExpiredError(f"Breeze session invalid: {e}") from e

    def _signed_headers(self, body: dict) -> dict:
        if not self.credentials.api_secret:
            raise BrokerError("Missing Breeze api secret in environment")
        if not self.account.session_token_enc:
            raise SessionExpiredError("No Breeze session; complete the login flow first")
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        payload = json.dumps(body, separators=(",", ":"))
        checksum = hashlib.sha256(
            (timestamp + payload + self.credentials.api_secret).encode()
        ).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-Checksum": f"token {checksum}",
            "X-Timestamp": timestamp,
            "X-AppKey": self.credentials.api_key or "",
            "X-SessionToken": decrypt_secret(self.account.session_token_enc),
        }

    def _session_key(self) -> str:
        """Identifies whose allowance this call spends.

        Breeze documents its limits per user, so the bucket is keyed by
        credential ref rather than by application.
        """
        return (self.account.credential_ref or str(self.account.id)).upper()

    async def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        body = body or {}
        # Breeze binds on two axes: a per-minute rate and a per-day total.
        # Exceeding them is documented as blocking the account rather than
        # returning something retryable, so both are enforced before the call
        # rather than reacted to afterwards.
        redis = get_redis()
        session_key = self._session_key()
        await breeze_limiter(redis, session_key=session_key).acquire()
        try:
            remaining = await breeze_quota(redis, session_key=session_key).take()
        except DailyQuotaExceeded as exc:
            raise BrokerError(str(exc)) from exc
        if remaining < 100:
            logger.warning(
                "breeze_daily_quota_low", remaining=remaining, session=session_key
            )
        headers = self._signed_headers(body)
        async with httpx.AsyncClient(base_url=API_BASE, timeout=15) as client:
            resp = await client.request(
                method, path, headers=headers, content=json.dumps(body, separators=(",", ":"))
            )
        data = resp.json()
        if resp.status_code == 401:
            raise SessionExpiredError("Breeze session rejected (401)")
        if resp.status_code >= 400 or data.get("Status") not in (200, None):
            raise BrokerError(f"Breeze error: {data.get('Error', data)}", raw=data)
        return data.get("Success") or {}

    # ── account data (scaffold, unverified) ──────────────────────────

    async def get_profile(self) -> BrokerProfile:
        data = await self._request("GET", "/customerdetails")
        return BrokerProfile(
            broker_client_id=str(data.get("idirect_userid", "")),
            name=data.get("idirect_user_name"),
            raw=data if isinstance(data, dict) else {},
        )

    async def get_funds(self) -> Funds:
        data = await self._request("GET", "/funds")
        return Funds(
            available_cash=Decimal(str(data.get("total_bank_balance", 0))),
            raw=data if isinstance(data, dict) else {},
        )

    async def get_holdings(self) -> list[Holding]:
        data = await self._request("GET", "/dematholdings")
        rows = data if isinstance(data, list) else []
        return [
            Holding(
                symbol=h.get("stock_code", ""),
                exchange=Exchange.NSE,
                quantity=int(h.get("quantity", 0) or 0),
                average_price=Decimal(str(h.get("average_price", 0) or 0)),
            )
            for h in rows
        ]

    async def get_positions(self) -> list[BrokerPosition]:
        data = await self._request("GET", "/portfoliopositions")
        rows = data if isinstance(data, list) else []
        return [
            BrokerPosition(
                symbol=p.get("stock_code", ""),
                exchange=Exchange(p.get("exchange_code", "NSE").upper()),
                product=ProductType.MIS,
                quantity=int(p.get("quantity", 0) or 0),
                average_price=Decimal(str(p.get("average_price", 0) or 0)),
            )
            for p in rows
        ]

    async def get_orders(self) -> list[BrokerOrder]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00.000Z")
        data = await self._request(
            "GET",
            "/order",
            {"exchange_code": "NSE", "from_date": today, "to_date": today},
        )
        rows = data if isinstance(data, list) else []
        return [
            BrokerOrder(
                broker_order_id=str(o.get("order_id", "")),
                symbol=o.get("stock_code", ""),
                exchange=Exchange(o.get("exchange_code", "NSE").upper()),
                side=OrderSide(o.get("action", "buy").upper()),
                order_type=OrderType(o.get("order_type", "market").upper()),
                product=ProductType.MIS,
                quantity=int(o.get("quantity", 0) or 0),
                status=_STATUS_MAP.get(o.get("status", ""), OrderStatus.OPEN),
                raw=o,
            )
            for o in rows
        ]

    # ── trading: blocked until verified against real account ────────

    async def place_order(self, request: OrderRequest, client_order_id: str) -> PlaceOrderResult:
        # TODO(breeze-live): POST /order with user_remark as idempotency hook.
        # Payload drafted per docs but unverified; refusing to send real orders.
        raise FeatureNotSupportedError(
            "Breeze order placement is scaffolded but unverified — refusing to send"
        )

    async def modify_order(self, broker_order_id: str, request: OrderRequest) -> PlaceOrderResult:
        raise FeatureNotSupportedError("Breeze modify_order scaffolded but unverified")

    async def cancel_order(self, broker_order_id: str) -> PlaceOrderResult:
        raise FeatureNotSupportedError("Breeze cancel_order scaffolded but unverified")

    async def get_instruments(self, exchange: str | None = None) -> list[Instrument]:
        # Breeze distributes a security master file; TODO(verify current URL).
        raise FeatureNotSupportedError("Breeze security master download not implemented yet")

    def subscribe_ticks(self, symbols: list[str]) -> AsyncIterator[Tick]:
        raise FeatureNotSupportedError("Breeze streaming (socket.io) not implemented yet")
