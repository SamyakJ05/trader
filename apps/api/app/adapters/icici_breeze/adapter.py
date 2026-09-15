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

import base64
import csv
import hashlib
import io
import json
import zipfile
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
from app.adapters.icici_breeze.stream import BreezeTickStream
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

# The security master, which maps Breeze's stock codes to exchange symbols.
# Two URLs are live and they are NOT mirrors: the SDK's MotherAppMaster zip
# carries MCX but no NSEScripMaster.txt, so NSE equities are simply absent from
# it. Verified by downloading both. The docs' NewSecurityMaster zip has the NSE
# file, so that is the one used.
SECURITY_MASTER_URL = "https://directlink.icicidirect.com/NewSecurityMaster/SecurityMaster.zip"

_SECURITY_MASTER_FILES = {
    "NSE": "NSEScripMaster.txt",
    "BSE": "BSEScripMaster.txt",
    "NFO": "FONSEScripMaster.txt",
}
LOGIN_BASE = "https://api.icicidirect.com/apiuser/login"

_STATUS_MAP = {
    "Ordered": OrderStatus.OPEN,
    "Requested": OrderStatus.SUBMITTED,
    "Executed": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELLED,
    "Rejected": OrderStatus.REJECTED,
    "Partially Executed": OrderStatus.PARTIALLY_FILLED,
}

# Breeze's product vocabulary is not Kite's and does not map cleanly onto it.
# Its documented values are futures, options, cash, mtf and btst — there is no
# "margin", and no cash-segment intraday product equivalent to Kite's MIS.
# MIS is therefore refused rather than silently sent as delivery, which would
# turn an intraday trade into one that settles and has to be funded.
_PRODUCT_MAP = {
    ProductType.CNC: "cash",
    ProductType.NRML: "futures",
}

_UNSUPPORTED_PRODUCT = {
    ProductType.MIS: (
        "Breeze has no cash-segment intraday product equivalent to MIS. "
        "Place this as CNC (delivery) or use a product Breeze supports; "
        "sending it as delivery silently would change what the trade is."
    ),
}

# Breeze's backing API accepts only limit and stoploss. Their own SDK fakes a
# market order by fetching a quote and computing an aggressive limit price
# client-side. We refuse instead: an "aggressive limit" derived from a quote we
# would have to fetch is a different order from the one the caller asked for,
# and quietly substituting it is not ours to decide.
_ORDER_TYPE_MAP = {
    OrderType.LIMIT: "limit",
    OrderType.SL: "stoploss",
    OrderType.SL_M: "stoploss",
}

_VALIDITY_MAP = {"DAY": "day", "IOC": "ioc"}


class BreezeAdapter(BrokerAdapter):
    broker = Broker.ICICI_BREEZE

    def login_url(self) -> str:
        if not self.credentials.api_key:
            raise BrokerError("Missing Breeze api key — set <credential_ref>_API_KEY in env")
        return f"{LOGIN_BASE}?api_key={quote_plus(self.credentials.api_key)}"

    async def connect(self) -> dict:
        return {"login_url": self.login_url(), "flow": "redirect"}

    async def exchange_session(self, api_session: str) -> dict:
        """Exchange the API_Session value from the redirect for a session token.

        Deliberately unsigned: this is the one Breeze call that does not carry
        the checksum headers, because the session it establishes is what those
        headers are built from. It also returns the user id, which every later
        request needs — the signature pairs it with the session key.
        """
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
        success = data["Success"]
        # Keep the user id: without it no later request can be signed.
        user_id = success.get("idirect_userid")
        if user_id:
            self.account.broker_client_id = str(user_id)
        return success

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
            # NOT the raw session token. Breeze expects
            # base64(user_id + ":" + session_key), the way HTTP Basic encodes a
            # credential pair. Sending the token verbatim fails every
            # authenticated call, and the error does not say why.
            "X-SessionToken": self._session_header(),
        }

    def _session_header(self) -> str:
        session_key = decrypt_secret(self.account.session_token_enc)
        user_id = self.account.broker_client_id
        if not user_id:
            raise SessionExpiredError(
                "Breeze needs the account's user id to sign requests; "
                "re-run the session exchange so it is stored"
            )
        pair = f"{user_id}:{session_key}".encode("ascii")
        return base64.b64encode(pair).decode("ascii")

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

    def _order_body(self, request: OrderRequest, client_order_id: str) -> dict:
        """Breeze's order payload. Field names follow their SDK's own
        place_order, which is the closest thing to ground truth."""
        refusal = _UNSUPPORTED_PRODUCT.get(request.product)
        if refusal:
            raise FeatureNotSupportedError(refusal)
        product = _PRODUCT_MAP.get(request.product)
        if product is None:
            raise FeatureNotSupportedError(
                f"Breeze has no product mapping for {request.product.value}"
            )
        order_type = _ORDER_TYPE_MAP.get(request.order_type)
        if order_type is None:
            raise FeatureNotSupportedError(
                f"Breeze does not accept {request.order_type.value} orders. "
                "Its API takes only limit and stoploss; a market order would "
                "have to be sent as an aggressive limit, which is a different "
                "order from the one requested."
            )
        if request.price is None:
            raise BrokerError("Breeze requires a price: every order is a limit order")

        body = {
            "stock_code": request.symbol,
            "exchange_code": request.exchange.value,
            "product": product,
            "action": request.side.value.lower(),
            "order_type": order_type,
            "quantity": str(request.quantity),
            "price": str(request.price),
            "validity": _VALIDITY_MAP.get(request.validity.value, "day"),
            # A label, not an idempotency key. Nothing in Breeze's SDK or docs
            # treats user_remark as one, so the platform's own idempotency
            # (client_order_id, unique per account) remains the only guard
            # against a duplicate order.
            "user_remark": client_order_id[:20],
        }
        if request.trigger_price is not None:
            body["stoploss"] = str(request.trigger_price)
        return body

    async def place_order(self, request: OrderRequest, client_order_id: str) -> PlaceOrderResult:
        # TODO(breeze-live-verification): payload follows Breeze's SDK but has
        # never been sent to their API. The live gate keeps this unreachable
        # until an operator verifies it against their own account.
        body = self._order_body(request, client_order_id)
        data = await self._request("POST", "/order", body)
        broker_order_id = data.get("order_id")
        if not broker_order_id:
            raise BrokerError("Breeze accepted the order without returning an id", raw=data)
        return PlaceOrderResult(
            broker_order_id=str(broker_order_id),
            status=OrderStatus.SUBMITTED,
            raw=data,
        )

    async def modify_order(self, broker_order_id: str, request: OrderRequest) -> PlaceOrderResult:
        order_type = _ORDER_TYPE_MAP.get(request.order_type)
        if order_type is None:
            raise FeatureNotSupportedError(
                f"Breeze does not accept {request.order_type.value} orders"
            )
        body = {
            "order_id": str(broker_order_id),
            "exchange_code": request.exchange.value,
            "order_type": order_type,
            "quantity": str(request.quantity),
            "price": str(request.price) if request.price is not None else "",
            "validity": _VALIDITY_MAP.get(request.validity.value, "day"),
        }
        if request.trigger_price is not None:
            body["stoploss"] = str(request.trigger_price)
        data = await self._request("PUT", "/order", body)
        return PlaceOrderResult(
            broker_order_id=str(broker_order_id),
            status=OrderStatus.OPEN,
            raw=data,
        )

    async def cancel_order(self, broker_order_id: str) -> PlaceOrderResult:
        data = await self._request(
            "DELETE",
            "/order",
            {"order_id": str(broker_order_id), "exchange_code": Exchange.NSE.value},
        )
        return PlaceOrderResult(
            broker_order_id=str(broker_order_id),
            status=OrderStatus.CANCELLED,
            raw=data,
        )

    async def get_instruments(self, exchange: str | None = None) -> list[Instrument]:
        """Breeze's security master: a zip of per-exchange CSVs.

        This is the only way to learn Breeze's stock codes — there is no API
        endpoint for the mapping, and the codes are ICICI's own (RELIANCE is
        RELIND). Regenerated daily around 08:00 IST.

        Downloaded here rather than at import time, which is what their SDK
        does: importing a module should not fetch several megabytes over the
        network before the caller has decided they need it.
        """
        exchange_code = (exchange or Exchange.NSE.value).upper()
        filename = _SECURITY_MASTER_FILES.get(exchange_code)
        if filename is None:
            raise FeatureNotSupportedError(
                f"No Breeze security master file known for {exchange_code}"
            )

        # Not through _request: the master is a static file on a different
        # host, needs no signature, and must not spend the API's rate budget.
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.get(SECURITY_MASTER_URL)
        if resp.status_code != 200:
            raise BrokerError(
                f"Breeze security master download failed ({resp.status_code})"
            )

        try:
            archive = zipfile.ZipFile(io.BytesIO(resp.content))
            with archive.open(filename) as handle:
                text = io.TextIOWrapper(handle, encoding="utf-8", errors="replace")
                rows = list(csv.DictReader(text))
        except (zipfile.BadZipFile, KeyError) as exc:
            raise BrokerError(
                f"Breeze security master is not readable: {exc}"
            ) from exc

        return [
            instrument
            for instrument in (self._instrument_from_row(row, exchange_code) for row in rows)
            if instrument is not None
        ]

    def _instrument_from_row(self, row: dict, exchange_code: str) -> Instrument | None:
        """One security-master row to an Instrument, or None if unusable.

        Column names in the master are quoted and inconsistently cased between
        files, so they are matched case-insensitively and stripped.
        """
        cleaned = {
            (key or "").strip().strip('"').lower(): (value or "").strip().strip('"')
            for key, value in row.items()
        }
        # ShortName is Breeze's own code — the one its API expects. ExchangeCode
        # is the NSE symbol, kept as the human-readable name so a user can tell
        # which company a code refers to.
        # Column names verified against a downloaded NSEScripMaster.txt:
        # Token, ShortName, Series, CompanyName, ticksize, Lotsize, ..., Symbol.
        # ShortName is Breeze's own code (RELIND); Symbol is the NSE ticker
        # (RELIANCE), kept alongside the company name so a human reading a
        # position can tell what RELIND is.
        stock_code = cleaned.get("shortname")
        if not stock_code:
            return None
        nse_symbol = cleaned.get("symbol")
        company = cleaned.get("companyname")
        name = f"{company} ({nse_symbol})" if company and nse_symbol else company or nse_symbol
        lot_size = cleaned.get("lotsize")
        tick_size = cleaned.get("ticksize")
        try:
            return Instrument(
                symbol=stock_code,
                exchange=Exchange(exchange_code),
                broker_token=cleaned.get("token") or None,
                name=name or None,
                lot_size=int(float(lot_size)) if lot_size else None,
                tick_size=Decimal(tick_size) if tick_size else None,
                instrument_type=cleaned.get("series") or None,
            )
        except (ValueError, ArithmeticError):
            # A malformed row is skipped rather than failing the whole sync:
            # the master carries tens of thousands of rows and one bad one
            # should not cost the rest.
            return None

    async def tick_feed(self, token_to_symbol: dict) -> BreezeTickStream:
        """A started Breeze tick stream.

        Takes the same mapping shape as the Kite feed so one supervisor can
        drive either, but Breeze subscribes by stock code rather than numeric
        token — the values are what it needs, and the keys are ignored.
        """
        if not self.account.session_token_enc:
            raise SessionExpiredError("Breeze ticks need a live session; connect first")
        if not self.account.broker_client_id:
            raise SessionExpiredError(
                "Breeze streaming needs the account's user id; re-run the session exchange"
            )
        stream = BreezeTickStream(
            user_id=self.account.broker_client_id,
            session_key=decrypt_secret(self.account.session_token_enc),
            stock_codes=sorted(set(token_to_symbol.values())),
        )
        await stream.start()
        return stream

    def subscribe_ticks(self, symbols: list[str]) -> AsyncIterator[Tick]:
        """Not supported through this interface.

        Kept refusing for the same reason as Kite's: the base interface hands
        back a bare iterator, but a socket.io connection has a lifetime that
        someone has to own and close. tick_feed returns the stream itself.
        """
        raise FeatureNotSupportedError(
            "Breeze streams have a lifetime to manage — use tick_feed()"
        )
