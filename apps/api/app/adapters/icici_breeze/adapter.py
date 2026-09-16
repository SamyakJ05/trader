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

import csv
import hashlib
import io
import json
import zipfile
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote_plus

import httpx

from app.adapters.base import (
    BrokerAdapter,
    BrokerError,
    FeatureNotSupportedError,
    SessionExpiredError,
    as_int,
)
from app.adapters.icici_breeze.stream import BreezeTickStream
from app.adapters.throttle import DailyQuotaExceeded, breeze_limiter, breeze_quota
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.core.security import decrypt_secret, encrypt_secret
from app.domain.calendar import IST
from app.domain.enums import (
    Broker,
    BrokerAccountStatus,
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
    # SL_M is deliberately absent. Breeze has one "stoploss" type, which
    # takes both a trigger and a limit price, so mapping SL_M here would
    # send a stop-loss LIMIT when a stop-loss MARKET was asked for -- the
    # same substitution this adapter already refuses for plain MARKET
    # orders. _order_body raises FeatureNotSupportedError instead.
}

_VALIDITY_MAP = {"DAY": "day", "IOC": "ioc"}

# Breeze's product_type as it comes BACK on a position, which is not the same
# vocabulary as the product names it accepts when placing one. Their docs show
# "Options" and "Future" capitalised on positions; lowercased here before
# lookup. "cash" is ambiguous between delivery and intraday -- Breeze does not
# distinguish -- and CNC is the conservative reading (see get_positions).
_POSITION_PRODUCT_MAP = {
    "cash": ProductType.CNC,
    "futures": ProductType.NRML,
    "future": ProductType.NRML,
    "options": ProductType.NRML,
    "option": ProductType.NRML,
    "fno": ProductType.NRML,
}

# Breeze's order_type as it comes BACK, which is not the vocabulary it
# accepts. "Stoploss" has no OrderType member -- OrderType("STOPLOSS") raises
# -- and it cannot be told apart from a stop-loss market from the response
# alone, so it reads as SL (the limit variant), matching what this adapter is
# willing to send.
_RESPONSE_ORDER_TYPE_MAP = {
    "limit": OrderType.LIMIT,
    "market": OrderType.MARKET,
    "stoploss": OrderType.SL,
    "stop loss": OrderType.SL,
    "sl": OrderType.SL,
}


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
        if not user_id:
            raise BrokerError(
                "Breeze returned no user id; requests cannot be signed without it",
                raw=data,
            )
        self.account.broker_client_id = str(user_id)

        session_key = success.get("session_token")
        if not session_key:
            raise BrokerError("Breeze returned no session token", raw=data)
        self.account.session_token_enc = encrypt_secret(str(session_key))
        # Breeze sessions die at midnight IST or 24 hours from issue, whichever
        # is first, and cannot be refreshed programmatically.
        midnight = (datetime.now(IST) + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        self.account.session_expires_at = min(
            midnight, datetime.now(IST) + timedelta(hours=24)
        ).astimezone(timezone.utc)
        self.account.status = BrokerAccountStatus.CONNECTED.value
        self.account.status_message = None
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
        # The value /customerdetails returns as session_token IS ALREADY
        # base64(user_id + ":" + the actual key) -- decoded a sample from
        # Breeze's own docs to confirm: "QUgzNzkzMDA6NDUwNTM0MjI=" decodes to
        # "AH379300:45053422", the pair itself, not a bare key that this
        # adapter should be pairing and encoding again. Doing so anyway wraps
        # it a second time, producing a header that still looks like valid
        # base64 (so nothing here would ever catch it) and decodes to
        # garbage on Breeze's end -- which is exactly what a real
        # "Invalid User Details" from a well-formed, correctly-checksummed
        # call turned out to be.
        session_key = decrypt_secret(self.account.session_token_enc)
        if not self.account.broker_client_id:
            raise SessionExpiredError(
                "Breeze needs the account's user id to sign requests; "
                "re-run the session exchange so it is stored"
            )
        return session_key

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
        # /customerdetails's SessionToken field takes the raw API_Session
        # value from the login redirect -- not the session_token that same
        # call returns, and definitely not the encrypted, already-exchanged
        # value stored on the account. Confirmed against Breeze's docs after
        # a real "Invalid session" from a signed, well-formed call: exchanging
        # is a one-way trip, by their design, so calling /customerdetails a
        # second time for the same login needs a value this adapter cannot
        # produce -- the raw API_Session is spent and never persisted (nor
        # should it be; it is a one-time credential).
        #
        # There is nothing to gain by trying anyway: exchange_session already
        # captured everything this endpoint would return -- broker_client_id
        # is idirect_userid, stored the moment the exchange succeeded. Once a
        # session exists, get_profile reports that rather than making a call
        # that cannot succeed by construction.
        if not self.account.broker_client_id:
            raise SessionExpiredError("No Breeze session; complete the login flow first")
        return BrokerProfile(
            broker_client_id=self.account.broker_client_id,
            name=None,
            raw={},
        )

    async def get_funds(self) -> Funds:
        # total_bank_balance is misleadingly named: confirmed against a real
        # account that it mirrors allocated_equity (money already committed
        # to equity trading), not free cash. unallocated_balance is the
        # figure that matched the operator's actual available balance in the
        # ICICI Direct app -- Breeze's own docs don't define either field
        # explicitly, so this was checked against reality rather than text.
        # unallocated_balance also arrives as a string while the other
        # numeric fields are floats -- Decimal(str(...)) handles both.
        data = await self._request("GET", "/funds")
        return Funds(
            available_cash=Decimal(str(data.get("unallocated_balance", 0))),
            raw=data if isinstance(data, dict) else {},
        )

    async def get_holdings(self) -> list[Holding]:
        # /dematholdings returns NO cost basis -- confirmed against the docs
        # and against a real account, where every row came back without an
        # average_price field at all. Reading one produced Decimal("0") for
        # every holding, which is not "unknown" but a claim that the stock
        # was free, making unrealized P&L equal the full notional. None says
        # what is actually true.
        #
        # `quantity` is the total on record; `demat_avail_quantity` is what
        # can be sold today, with the rest pledged, blocked or allocated.
        # The docs' own sample has quantity=1 against demat_avail_quantity=0.
        # Sizing a sell off the total is the same total-vs-free trap as
        # total_bank_balance was for funds.
        data = await self._request("GET", "/dematholdings")
        rows = data if isinstance(data, list) else []
        return [
            Holding(
                symbol=h.get("stock_code", ""),
                exchange=Exchange.NSE,
                quantity=as_int(h.get("demat_avail_quantity")),
                total_quantity=as_int(h.get("quantity")),
                average_price=None,
            )
            for h in rows
        ]

    async def get_positions(self) -> list[BrokerPosition]:
        # product was hardcoded to MIS for every row, discarding Breeze's own
        # product_type. A CNC delivery holding came back labelled intraday,
        # and anything that squares off MIS positions before the close would
        # have liquidated a position meant to be held.
        #
        # The mapping is imperfect and deliberately conservative: Breeze's
        # "cash" covers both delivery and intraday equity, and it does not
        # say which. CNC is the safe reading -- treating a real intraday
        # position as delivery leaves it open, while the reverse sells
        # something the operator meant to keep.
        data = await self._request("GET", "/portfoliopositions")
        rows = data if isinstance(data, list) else []
        out: list[BrokerPosition] = []
        for p in rows:
            try:
                exchange = Exchange(str(p.get("exchange_code") or "NSE").upper())
            except ValueError:
                # One unrecognised segment must not blind the platform to
                # every other position. Skip the row, say so, keep going.
                logger.warning(
                    "breeze_position_unknown_exchange",
                    exchange=p.get("exchange_code"),
                    symbol=p.get("stock_code"),
                )
                continue
            out.append(
                BrokerPosition(
                    symbol=p.get("stock_code", ""),
                    exchange=exchange,
                    product=_POSITION_PRODUCT_MAP.get(
                        str(p.get("product_type") or "").lower(), ProductType.CNC
                    ),
                    quantity=as_int(p.get("quantity")),
                    average_price=Decimal(str(p.get("average_price", 0) or 0)),
                )
            )
        return out

    async def get_orders(self) -> list[BrokerOrder]:
        # The trading day is an IST day. Deriving it from UTC put the window
        # on the previous calendar date for everything before 05:30 IST, so a
        # pre-open sync asked for yesterday's book.
        today = datetime.now(IST).strftime("%Y-%m-%dT00:00:00.000Z")
        data = await self._request(
            "GET",
            "/order",
            {"exchange_code": "NSE", "from_date": today, "to_date": today},
        )
        rows = data if isinstance(data, list) else []
        out: list[BrokerOrder] = []
        for o in rows:
            # Every enum cast here is on a broker-supplied string, inside what
            # used to be a list comprehension: one unrecognised value raised
            # ValueError and took down the whole order-book sync rather than
            # skipping a row. Breeze returns order_type "Stoploss", which has
            # no OrderType member at all, so a single resting stop-loss order
            # was enough to blind the platform to every other order.
            try:
                exchange = Exchange(str(o.get("exchange_code") or "NSE").upper())
                side = OrderSide(str(o.get("action") or "buy").upper())
            except ValueError:
                logger.warning(
                    "breeze_order_unparsable",
                    order_id=o.get("order_id"),
                    exchange=o.get("exchange_code"),
                    action=o.get("action"),
                )
                continue
            out.append(
                BrokerOrder(
                    broker_order_id=str(o.get("order_id", "")),
                    symbol=o.get("stock_code", ""),
                    exchange=exchange,
                    side=side,
                    order_type=_RESPONSE_ORDER_TYPE_MAP.get(
                        str(o.get("order_type") or "").lower(), OrderType.LIMIT
                    ),
                    product=_POSITION_PRODUCT_MAP.get(
                        str(o.get("product_type") or "").lower(), ProductType.CNC
                    ),
                    quantity=as_int(o.get("quantity")),
                    status=_STATUS_MAP.get(o.get("status", ""), OrderStatus.OPEN),
                    raw=o,
                )
            )
        return out

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
                "Its API takes only limit and stoploss. A market order would "
                "have to be sent as an aggressive limit, and a stop-loss "
                "market as a stop-loss limit — both are different orders "
                "from the one requested."
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

    # SEBI's algo framework (circular of 4 Feb 2025, universal from 1 Apr 2026)
    # requires order requests to originate from an IP the broker has
    # whitelisted. The requirement is confined to the transactional layer:
    # placing, modifying, cancelling and squaring off. Market data, the order
    # book, positions and the websocket stream are reachable from anywhere,
    # and the daily browser login may be done from any machine — its session
    # key is then handed to whatever host holds the registered IP.
    #
    # Practically: everything but these three methods can be exercised from a
    # laptop. Only they need the registered host.
    _STATIC_IP_HINT = (
        "Orders must originate from the static IP registered with ICICI for "
        "this app. Reads, market data and the websocket are not restricted, so "
        "a failure here while everything else works points at the IP rather "
        "than the session or the payload."
    )

    async def place_order(self, request: OrderRequest, client_order_id: str) -> PlaceOrderResult:
        # TODO(breeze-live-verification): payload follows Breeze's SDK but has
        # never been sent to their API. The live gate keeps this unreachable
        # until an operator verifies it against their own account.
        body = self._order_body(request, client_order_id)
        try:
            data = await self._request("POST", "/order", body)
        except BrokerError as exc:
            # A blocked-by-IP rejection arrives as an ordinary broker error.
            # Saying so here saves hours spent re-checking the session and the
            # payload, which is where suspicion naturally falls first.
            raise BrokerError(f"{exc} — {self._STATIC_IP_HINT}") from exc
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
        try:
            data = await self._request("PUT", "/order", body)
        except BrokerError as exc:
            raise BrokerError(f"{exc} — {self._STATIC_IP_HINT}") from exc
        return PlaceOrderResult(
            broker_order_id=str(broker_order_id),
            status=OrderStatus.OPEN,
            raw=data,
        )

    async def cancel_order(self, broker_order_id: str) -> PlaceOrderResult:
        # exchange_code is required on DELETE /order and hardcoded to NSE
        # here, because the adapter interface passes only a broker order id
        # and this adapter keeps no per-order exchange memory. An NFO order
        # therefore cannot be cancelled through this method -- the one
        # situation where cancelling matters most.
        #
        # Not fixed here: carrying the exchange would change
        # BrokerAdapter.cancel_order's signature across all four adapters and
        # its call site. It is also not currently reachable -- _order_body
        # omits the expiry/strike/right fields Breeze requires for
        # derivatives, so no F&O order can be placed through this adapter to
        # begin with. Both belong in the same change, before F&O is enabled.
        try:
            data = await self._request(
                "DELETE",
                "/order",
                {"order_id": str(broker_order_id), "exchange_code": Exchange.NSE.value},
            )
        except BrokerError as exc:
            raise BrokerError(f"{exc} — {self._STATIC_IP_HINT}") from exc
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
        drive either. BOTH halves matter here: Breeze subscribes by numeric
        security-master token (the keys), and every tick comes back
        identified only by the room that token names, so the stock code (the
        values) is how a tick is matched to an instrument.

        A previous version passed only the values, on the belief that Breeze
        subscribed by stock code. It does not, and the feed was silent.
        """
        if not self.account.session_token_enc:
            raise SessionExpiredError("Breeze ticks need a live session; connect first")
        if not self.account.broker_client_id:
            raise SessionExpiredError(
                "Breeze streaming needs the account's user id; re-run the session exchange"
            )
        if not token_to_symbol:
            raise BrokerError(
                "Breeze ticks need instrument tokens. Sync the security master "
                "first — without it there is nothing to subscribe to."
            )
        stream = BreezeTickStream(
            user_id=self.account.broker_client_id,
            session_key=decrypt_secret(self.account.session_token_enc),
            token_to_symbol={str(k): str(v) for k, v in token_to_symbol.items()},
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
