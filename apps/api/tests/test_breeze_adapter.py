"""ICICI Breeze adapter.

Breeze differs from Kite in ways a Kite-first adapter gets silently wrong: the
session header is a base64 credential pair rather than a token, its product
vocabulary is not Kite's, and its API has no market order at all. These tests
pin the differences.

Nothing here reaches the network; the adapter is still SCAFFOLD.
"""

import base64
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.adapters.base import BrokerError, FeatureNotSupportedError, SessionExpiredError
from app.adapters.icici_breeze.adapter import BreezeAdapter
from app.core.config import BrokerEnvCredentials
from app.domain.enums import Exchange, OrderSide, OrderType, ProductType, Validity
from app.domain.models import OrderRequest


def account(session="plain:sess-key", user_id="ICICI123"):
    return SimpleNamespace(
        id="acct", broker="icici_breeze", credential_ref="BREEZE_MAIN",
        session_token_enc=session, broker_client_id=user_id, environment="paper",
    )


def adapter(monkeypatch, **kw):
    monkeypatch.setenv("BREEZE_MAIN_API_KEY", "app-key")
    monkeypatch.setenv("BREEZE_MAIN_API_SECRET", "secret")
    return BreezeAdapter(account(**kw), BrokerEnvCredentials("BREEZE_MAIN"))


def order(product=ProductType.CNC, order_type=OrderType.LIMIT, price=Decimal("2845.50"), **kw):
    defaults = dict(
        symbol="RELIND", exchange=Exchange.NSE, side=OrderSide.BUY,
        order_type=order_type, product=product, quantity=10, price=price,
        validity=Validity.DAY,
    )
    defaults.update(kw)
    return OrderRequest(**defaults)


# ── the session header ───────────────────────────────────────────────


def test_the_session_header_is_a_base64_credential_pair(monkeypatch):
    """Breeze wants base64(user_id:session_key), the way HTTP Basic encodes a
    pair. Sending the token verbatim — which a Kite-shaped adapter would —
    fails every authenticated call, and the error does not say why."""
    headers = adapter(monkeypatch)._signed_headers({})
    decoded = base64.b64decode(headers["X-SessionToken"]).decode()
    assert decoded == "ICICI123:sess-key"


def test_signing_without_a_user_id_is_refused(monkeypatch):
    """The user id comes from the session exchange. Without it no request can
    be signed, and saying so beats an opaque 401."""
    with pytest.raises(SessionExpiredError, match="user id"):
        adapter(monkeypatch, user_id=None)._signed_headers({})


def test_the_checksum_covers_timestamp_body_and_secret(monkeypatch):
    import hashlib

    a = adapter(monkeypatch)
    headers = a._signed_headers({"a": 1})
    expected = hashlib.sha256(
        (headers["X-Timestamp"] + '{"a":1}' + "secret").encode()
    ).hexdigest()
    assert headers["X-Checksum"] == f"token {expected}"


def test_the_timestamp_has_the_exact_shape_breeze_expects(monkeypatch):
    """Truncated to seconds with a literal .000Z — not real milliseconds.
    A different shape changes the checksum input and fails authentication."""
    stamp = adapter(monkeypatch)._signed_headers({})["X-Timestamp"]
    assert stamp.endswith(".000Z") and len(stamp) == 24


# ── products ─────────────────────────────────────────────────────────


def test_delivery_maps_to_cash(monkeypatch):
    body = adapter(monkeypatch)._order_body(order(product=ProductType.CNC), "cli-1")
    assert body["product"] == "cash"


def test_intraday_is_refused_rather_than_sent_as_delivery(monkeypatch):
    """Breeze has no cash-segment intraday product. Sending MIS as delivery
    would turn an intraday trade into one that settles and must be funded —
    a different trade from the one requested."""
    with pytest.raises(FeatureNotSupportedError, match="intraday"):
        adapter(monkeypatch)._order_body(order(product=ProductType.MIS), "cli-1")


# ── order types ──────────────────────────────────────────────────────


def test_a_market_order_is_refused(monkeypatch):
    """Breeze's API takes only limit and stoploss. Their SDK fakes a market
    order with a client-computed aggressive limit price; substituting that
    silently would place a different order from the one asked for."""
    with pytest.raises(FeatureNotSupportedError, match="market order"):
        adapter(monkeypatch)._order_body(
            order(order_type=OrderType.MARKET, price=None), "cli-1"
        )


def test_a_priceless_order_is_refused_at_the_adapter_too(monkeypatch):
    """OrderRequest already rejects a LIMIT order with no price, so this guard
    is a backstop rather than the first line. It matters because every Breeze
    order is a limit order: a product or order type that reached here without
    a price would otherwise be sent priced as an empty string."""
    priceless = SimpleNamespace(
        symbol="RELIND", exchange=Exchange.NSE, side=OrderSide.BUY,
        order_type=OrderType.LIMIT, product=ProductType.CNC, quantity=10,
        price=None, trigger_price=None, validity=Validity.DAY,
    )
    with pytest.raises(BrokerError, match="price"):
        adapter(monkeypatch)._order_body(priceless, "cli-1")


def test_a_stoploss_order_carries_its_trigger(monkeypatch):
    body = adapter(monkeypatch)._order_body(
        order(order_type=OrderType.SL, trigger_price=Decimal("2800")), "cli-1"
    )
    assert body["order_type"] == "stoploss"
    assert body["stoploss"] == "2800"


# ── payload shape ────────────────────────────────────────────────────


def test_the_payload_uses_breeze_field_names(monkeypatch):
    body = adapter(monkeypatch)._order_body(order(), "cli-1")
    assert body["stock_code"] == "RELIND"
    assert body["exchange_code"] == "NSE"
    assert body["action"] == "buy"
    assert body["quantity"] == "10"


def test_numbers_are_sent_as_strings(monkeypatch):
    """Breeze's SDK sends every numeric field as a string; sending real
    numbers changes the serialized body and therefore the checksum."""
    body = adapter(monkeypatch)._order_body(order(), "cli-1")
    assert all(isinstance(body[k], str) for k in ("quantity", "price"))


def test_user_remark_carries_our_client_order_id(monkeypatch):
    """A label, not an idempotency key — nothing in Breeze treats it as one,
    so our own per-account uniqueness stays the only duplicate guard."""
    body = adapter(monkeypatch)._order_body(order(), "client-order-abc")
    assert body["user_remark"].startswith("client-order-abc"[:20])


# ── the security master ──────────────────────────────────────────────
# Breeze's stock codes exist only in this file; there is no API for the
# mapping. Rows below are taken verbatim from a downloaded NSEScripMaster.txt.


def master_row(**overrides):
    """A real row's shape: quoted keys, inconsistent spacing and casing."""
    row = {
        '"Token"': '"2885"',
        ' "ShortName"': '"RELIND"',
        ' "Series"': '"EQ"',
        ' "CompanyName"': '"RELIANCE INDUSTRIES"',
        ' "ticksize"': "0.01",
        ' "Lotsize"': "1",
        ' "Symbol"': '"RELIANCE"',
    }
    row.update(overrides)
    return row


def test_a_master_row_yields_breezes_own_code(monkeypatch):
    """The symbol stored is ShortName — what Breeze's API expects — not the
    NSE ticker a Kite-shaped adapter would reach for."""
    instrument = adapter(monkeypatch)._instrument_from_row(master_row(), "NSE")
    assert instrument.symbol == "RELIND"
    assert instrument.broker_token == "2885"


def test_the_name_carries_the_nse_symbol_so_a_code_is_legible(monkeypatch):
    """RELIND means nothing to a human reading a position list."""
    instrument = adapter(monkeypatch)._instrument_from_row(master_row(), "NSE")
    assert "RELIANCE INDUSTRIES" in instrument.name
    assert "RELIANCE" in instrument.name


def test_lot_and_tick_size_are_parsed(monkeypatch):
    instrument = adapter(monkeypatch)._instrument_from_row(master_row(), "NSE")
    assert instrument.lot_size == 1
    assert instrument.tick_size == Decimal("0.01")


def test_a_row_without_a_short_name_is_skipped(monkeypatch):
    """No code means nothing we could trade; better skipped than stored with
    an empty symbol that silently matches nothing."""
    assert adapter(monkeypatch)._instrument_from_row(
        master_row(**{' "ShortName"': '""'}), "NSE"
    ) is None


def test_a_malformed_row_does_not_fail_the_whole_sync(monkeypatch):
    """The master has tens of thousands of rows; one bad one should not cost
    the rest."""
    assert adapter(monkeypatch)._instrument_from_row(
        master_row(**{' "Lotsize"': '"not-a-number"'}), "NSE"
    ) is None


async def test_an_unknown_exchange_is_refused(monkeypatch):
    """Each exchange is a separate file inside the zip; asking for one we have
    no filename for should say so rather than download 2MB and find nothing."""
    with pytest.raises(FeatureNotSupportedError, match="security master"):
        await adapter(monkeypatch).get_instruments("MCX")


def test_the_master_url_is_the_one_carrying_nse_equities():
    """Two URLs are live and they are not mirrors: the SDK's MotherAppMaster
    zip has MCX but no NSEScripMaster.txt, so NSE equities are absent from it
    entirely. Verified by downloading both."""
    from app.adapters.icici_breeze.adapter import SECURITY_MASTER_URL

    assert "NewSecurityMaster" in SECURITY_MASTER_URL


# ── the session exchange ─────────────────────────────────────────────
# What a Breeze user has after logging in is an API_Session from the redirect
# URL, not a session token. The exchange turns one into the other, and is the
# only place the user id comes from — without which nothing can be signed.


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def patch_exchange(monkeypatch, payload, status_code=200):
    import httpx

    async def fake_request(self, *args, **kwargs):
        return FakeResponse(payload, status_code)

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)


async def test_the_exchange_stores_the_session_and_the_user_id(monkeypatch):
    a = adapter(monkeypatch, session=None, user_id=None)
    patch_exchange(
        monkeypatch,
        {"Success": {"idirect_userid": "ICICI999", "session_token": "real-key"}},
    )
    await a.exchange_session("api-session-from-redirect")

    assert a.account.broker_client_id == "ICICI999"
    assert a.account.session_token_enc, "the real session key must be stored"
    assert "real-key" not in a.account.session_token_enc, "and stored encrypted"


async def test_the_exchange_sets_an_expiry_before_midnight(monkeypatch):
    """Breeze sessions die at midnight IST or 24 hours from issue, whichever
    is first, and cannot be refreshed programmatically."""
    from datetime import datetime, timedelta, timezone

    a = adapter(monkeypatch, session=None, user_id=None)
    patch_exchange(
        monkeypatch,
        {"Success": {"idirect_userid": "ICICI999", "session_token": "real-key"}},
    )
    await a.exchange_session("api-session")

    expires = a.account.session_expires_at
    assert expires is not None
    assert expires <= datetime.now(timezone.utc) + timedelta(hours=24, minutes=1)


async def test_an_exchange_without_a_user_id_is_refused(monkeypatch):
    """A session we cannot sign with is worse than none: it would look
    connected and fail every call."""
    a = adapter(monkeypatch, session=None, user_id=None)
    patch_exchange(monkeypatch, {"Success": {"session_token": "key"}})
    with pytest.raises(BrokerError, match="user id"):
        await a.exchange_session("api-session")


async def test_an_exchange_without_a_session_token_is_refused(monkeypatch):
    a = adapter(monkeypatch, session=None, user_id=None)
    patch_exchange(monkeypatch, {"Success": {"idirect_userid": "ICICI999"}})
    with pytest.raises(BrokerError, match="session token"):
        await a.exchange_session("api-session")


# ── /customerdetails takes a different shape than every other endpoint ─
# Breeze's docs: no checksum headers on this one, SessionToken/AppKey as
# body fields instead. Sending it through the generic signed _request (empty
# body, checksum headers attached) produced a live "Request Object is Null"
# from Breeze — this pins the correct shape so it cannot regress silently.


async def test_get_profile_sends_session_and_app_key_in_the_body(monkeypatch):
    import fakeredis.aioredis

    from app.adapters.icici_breeze import adapter as adapter_module

    a = adapter(monkeypatch)
    monkeypatch.setattr(
        adapter_module, "get_redis", lambda: fakeredis.aioredis.FakeRedis(decode_responses=True)
    )
    captured = {}

    async def fake_request(self, method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["headers"] = kwargs.get("headers")
        captured["json"] = kwargs.get("json")
        return FakeResponse({"Success": {"idirect_userid": "ICICI123", "idirect_user_name": "Someone"}})

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    profile = await a.get_profile()

    assert captured["path"] == "/customerdetails"
    assert captured["json"] == {"SessionToken": "sess-key", "AppKey": "app-key"}
    assert captured["headers"] is None, (
        "customerdetails takes no headers at all per Breeze's docs — sending "
        "the checksum headers here is the bug this test exists to catch"
    )
    assert profile.broker_client_id == "ICICI123"


async def test_get_profile_without_a_session_is_refused(monkeypatch):
    a = adapter(monkeypatch, session=None, user_id=None)
    with pytest.raises(SessionExpiredError, match="login flow"):
        await a.get_profile()


async def test_get_profile_surfaces_breezes_error_text(monkeypatch):
    import fakeredis.aioredis

    from app.adapters.icici_breeze import adapter as adapter_module

    a = adapter(monkeypatch)
    monkeypatch.setattr(
        adapter_module, "get_redis", lambda: fakeredis.aioredis.FakeRedis(decode_responses=True)
    )
    patch_exchange(monkeypatch, {"Error": "Request Object is Null"}, status_code=200)

    with pytest.raises(BrokerError, match="Request Object is Null"):
        await a.get_profile()


# ── the static IP requirement ────────────────────────────────────────
# SEBI's algo framework confines the whitelisted-IP rule to the transactional
# layer. Reads and streaming work from anywhere, which is what lets the
# playbook's first four stages run from a laptop.


async def test_an_order_failure_names_the_static_ip_as_a_suspect(monkeypatch):
    """A blocked-by-IP rejection arrives as an ordinary broker error.
    Suspicion naturally falls on the session or the payload first, and hours
    go into re-checking those."""
    a = adapter(monkeypatch)

    async def refuse(*args, **kwargs):
        raise BrokerError("Breeze error: request rejected")

    monkeypatch.setattr(BreezeAdapter, "_request", refuse)
    with pytest.raises(BrokerError, match="static IP"):
        await a.place_order(order(), "cli-1")


async def test_cancelling_carries_the_same_hint(monkeypatch):
    a = adapter(monkeypatch)

    async def refuse(*args, **kwargs):
        raise BrokerError("Breeze error: request rejected")

    monkeypatch.setattr(BreezeAdapter, "_request", refuse)
    with pytest.raises(BrokerError, match="static IP"):
        await a.cancel_order("123")


async def test_a_read_failure_does_not_blame_the_ip(monkeypatch):
    """Reads are not IP-restricted, so pointing at the IP here would send
    someone chasing the wrong cause."""
    a = adapter(monkeypatch)

    async def refuse(*args, **kwargs):
        raise BrokerError("Breeze error: session expired")

    monkeypatch.setattr(BreezeAdapter, "_request", refuse)
    with pytest.raises(BrokerError) as exc:
        await a.get_funds()
    assert "static IP" not in str(exc.value)
