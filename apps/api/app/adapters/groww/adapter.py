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
    as_int,
)
from app.core.logging import get_logger
from app.core.security import decrypt_secret
from app.domain.enums import Broker, Exchange, OrderSide, OrderStatus, OrderType, ProductType
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

API_BASE = "https://api.groww.in/v1"

_STATUS_MAP = {
    "NEW": OrderStatus.SUBMITTED,
    "ACKED": OrderStatus.OPEN,
    "APPROVED": OrderStatus.OPEN,
    "OPEN": OrderStatus.OPEN,
    "TRIGGER_PENDING": OrderStatus.OPEN,
    "EXECUTED": OrderStatus.FILLED,
    "COMPLETED": OrderStatus.FILLED,
    "DELIVERY_AWAITED": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
    "FAILED": OrderStatus.FAILED,
}


class GrowwAdapter(BrokerAdapter):
    broker = Broker.GROWW

    def _token(self) -> str:
        """The freshest token available, stored session first.

        Groww access tokens expire daily at 06:00. The env var used to win
        unconditionally, which made a stale one permanent: re-authenticating
        through the app writes session_token_enc, which was never read while
        the env var was set, so every call kept 401ing and every reconnect
        appeared to succeed. The env var is the bootstrap -- a token pasted
        from the dashboard to get started -- and anything obtained since is
        newer by definition.
        """
        if self.account.session_token_enc:
            return decrypt_secret(self.account.session_token_enc)
        if self.credentials.access_token:
            return self.credentials.access_token
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
        try:
            body = resp.json()
        except ValueError as exc:
            # A 502/503 from the edge is an HTML page, and json() raises
            # JSONDecodeError -- not a BrokerError, so it escaped every
            # handler in the service layer as an unhandled 500 rather than a
            # broker failure the account status could record.
            raise BrokerError(
                f"Groww returned a non-JSON response (HTTP {resp.status_code})",
                retryable=resp.status_code >= 500,
            ) from exc
        if resp.status_code >= 400:
            raise BrokerError(f"Groww error: {body}", raw=body)
        # Groww's failure envelope is {"status": "FAILURE", "error": {...}}
        # and can arrive with HTTP 200. Nothing here used to look at it, so
        # an error fell through to `body.get("payload", body)` -- which
        # returns the error body itself, and callers then read .get("holdings",
        # [])` off it and saw an empty list. An error became "you hold
        # nothing", which is the most dangerous possible default.
        if isinstance(body, dict) and str(body.get("status", "")).upper() == "FAILURE":
            error = body.get("error") or {}
            raise BrokerError(
                f"Groww error: {error.get('message') or error or body}", raw=body
            )
        if not isinstance(body, dict) or "payload" not in body:
            # Shape surprise. Coercing it to {} would read as "no data".
            raise BrokerError(f"Unexpected Groww response shape: {body!r}", raw=body if isinstance(body, dict) else {})
        return body["payload"]

    # ── account data (scaffold: paths per docs, unverified) ──────────

    async def get_profile(self) -> BrokerProfile:
        """Groww publishes no user-profile endpoint.

        This called GET /user/profile, which does not exist in their Trading
        API -- the documented surface is margins, portfolio, orders, live data
        and instruments. Since connect(), refresh_session() and
        verify_read_access() all gate on this, a correctly-configured account
        with a valid token could not connect at all, and the 404 sent the
        operator hunting a token problem that was not there.

        Margins is the cheapest call that proves the token works, so health
        checks run through it. It carries no user id, so there is none to
        report; the field is left empty rather than invented.
        """
        data = await self._request("GET", "/margins/detail/user")
        return BrokerProfile(broker_client_id="", name=None, raw=data)

    async def get_funds(self) -> Funds:
        # The fallback used to be `net_margin`, which is wrong twice over:
        # the real field is net_margin_used, so it never fired, and margin
        # USED is the opposite of available cash -- had the key been right, a
        # missing clear_cash would have reported deployed margin as free
        # money. Falling back to zero is the honest failure: it refuses to
        # trade rather than inventing headroom.
        #
        # clear_cash itself is unverified against a real account. Groww also
        # reports per-segment figures (equity_margin_details.cnc_balance_
        # available and friends) which may be the number a trader actually
        # spends from; which one is right needs a real account to settle.
        data = await self._request("GET", "/margins/detail/user")  # TODO(verify path)
        return Funds(
            available_cash=Decimal(str(data.get("clear_cash", 0) or 0)),
            raw=data,
        )

    async def get_holdings(self) -> list[Holding]:
        # `quantity` is the total on record. Groww reports demat_free_quantity
        # alongside pledge_quantity, demat_locked_quantity and t1_quantity --
        # sizing a sell off the total counts pledged and locked stock as
        # sellable. Same trap as Breeze's dematholdings.
        #
        # Exchange is hardcoded NSE because Groww's holdings payload carries
        # no exchange field at all, only isin and trading_symbol. That is a
        # forced guess, wrong for a BSE-only holding, and resolving it needs
        # an isin lookup against the instruments table.
        data = await self._request("GET", "/holdings/user")  # TODO(verify path)
        return [
            Holding(
                symbol=h.get("trading_symbol", h.get("symbol", "")),
                exchange=Exchange.NSE,
                quantity=as_int(h.get("demat_free_quantity", h.get("quantity"))),
                total_quantity=as_int(h.get("quantity")),
                average_price=Decimal(str(h.get("average_price", 0) or 0)),
            )
            for h in data.get("holdings", [])
        ]

    async def get_positions(self) -> list[BrokerPosition]:
        # `average_price` is not a field Groww returns on a position -- its
        # documented price fields are credit_price, debit_price, net_price and
        # the carry_forward_* pair. Reading the absent one gave every position
        # a cost basis of zero, which makes unrealized P&L the full notional.
        # net_price is the intended figure; unverified against a real account.
        #
        # Every enum cast here was unguarded inside a list comprehension: one
        # product or exchange value outside our enums raised ValueError and
        # took down the whole fetch rather than skipping a row.
        data = await self._request("GET", "/positions/user")  # TODO(verify path)
        out: list[BrokerPosition] = []
        for p in data.get("positions", []):
            try:
                exchange = Exchange(str(p.get("exchange") or "NSE").upper())
            except ValueError:
                logger.warning(
                    "groww_position_unknown_exchange",
                    exchange=p.get("exchange"),
                    symbol=p.get("trading_symbol"),
                )
                continue
            try:
                product = ProductType(str(p.get("product") or "MIS").upper())
            except ValueError:
                logger.warning(
                    "groww_position_unknown_product",
                    product=p.get("product"),
                    symbol=p.get("trading_symbol"),
                )
                continue
            out.append(
                BrokerPosition(
                    symbol=p.get("trading_symbol", ""),
                    exchange=exchange,
                    product=product,
                    quantity=as_int(p.get("quantity")),
                    average_price=Decimal(str(p.get("net_price", 0) or 0)),
                )
            )
        return out

    async def get_orders(self) -> list[BrokerOrder]:
        data = await self._request("GET", "/order/list")  # TODO(verify path)
        out: list[BrokerOrder] = []
        for o in data.get("order_list", []):
            try:
                exchange = Exchange(str(o.get("exchange") or "NSE").upper())
                side = OrderSide(str(o.get("transaction_type") or "BUY").upper())
                order_type = OrderType(str(o.get("order_type") or "MARKET").upper())
                product = ProductType(str(o.get("product") or "MIS").upper())
            except ValueError:
                logger.warning(
                    "groww_order_unparsable",
                    order_id=o.get("groww_order_id"),
                    exchange=o.get("exchange"),
                    order_type=o.get("order_type"),
                    product=o.get("product"),
                )
                continue
            raw_status = str(o.get("order_status") or "")
            status = _STATUS_MAP.get(raw_status)
            if status is None:
                # Defaulting an unknown status to OPEN claims a live working
                # order exists. For a terminal state Groww added since this
                # map was written -- FAILED was one -- that is a phantom order
                # the platform will never retry and may double-count exposure
                # against. Refusing to guess is the conservative reading.
                logger.warning(
                    "groww_order_unknown_status",
                    order_id=o.get("groww_order_id"),
                    status=raw_status,
                )
                continue
            out.append(
                BrokerOrder(
                    broker_order_id=str(o.get("groww_order_id", "")),
                    symbol=o.get("trading_symbol", ""),
                    exchange=exchange,
                    side=side,
                    order_type=order_type,
                    product=product,
                    quantity=as_int(o.get("quantity")),
                    filled_quantity=as_int(o.get("filled_quantity")),
                    status=status,
                    raw=o,
                )
            )
        return out

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
