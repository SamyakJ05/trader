"""ICICI Breeze adapter.

Breeze differs from Kite in ways a Kite-first adapter gets silently wrong: the
session header is a base64 credential pair rather than a token, its product
vocabulary is not Kite's, and its API has no market order at all. These tests
pin the differences.

Nothing here reaches the network; the adapter is still SCAFFOLD.
"""

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
    a = BreezeAdapter(account(**kw), BrokerEnvCredentials("BREEZE_MAIN"))

    # Transactional calls pace against Redis first (ICICI's documented cap of
    # 10 orders/second). No test here is about that pacing, and a real Redis
    # is not available; the throttle stays unconditional in the adapter and
    # test_breeze_derivatives asserts it is applied.
    async def no_throttle():
        return None

    a._throttle_order = no_throttle
    return a


def order(product=ProductType.CNC, order_type=OrderType.LIMIT, price=Decimal("2845.50"), **kw):
    defaults = dict(
        symbol="RELIND", exchange=Exchange.NSE, side=OrderSide.BUY,
        order_type=order_type, product=product, quantity=10, price=price,
        validity=Validity.DAY,
    )
    defaults.update(kw)
    return OrderRequest(**defaults)


# ── the session header ───────────────────────────────────────────────


def test_the_session_header_is_the_stored_session_key_verbatim(monkeypatch):
    """Regression: this used to re-encode base64(user_id:session_key), on the
    assumption Breeze wanted an HTTP-Basic-style pair built here. It does
    not — decoding a real sample from Breeze's own docs
    ("QUgzNzkzMDA6NDUwNTM0MjI=" -> "AH379300:45053422") shows the
    session_token /customerdetails returns IS ALREADY that encoded pair.
    Re-encoding it wrapped it a second time: syntactically valid base64, so
    nothing here ever caught it, but garbage once Breeze decoded it — the
    live cause of a well-formed, correctly-checksummed call failing with
    "Invalid User Details". The fix is to do nothing to it."""
    headers = adapter(monkeypatch)._signed_headers({})
    assert headers["X-SessionToken"] == "sess-key"


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
    so our own per-account uniqueness stays the only duplicate guard.

    Carried with its punctuation removed: Breeze refuses a remark that is not
    purely alphanumeric, which this test asserted the opposite of until a real
    order came back "Only alphanumeric characters are allowed in user_remark".
    See test_breeze_user_remark.py."""
    body = adapter(monkeypatch)._order_body(order(), "client-order-abc")
    assert body["user_remark"] == "clientorderabc"
    assert body["user_remark"].isalnum()


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


# ── get_profile reports the exchange's own result, not a fresh call ────
# /customerdetails's SessionToken field wants the raw, one-time API_Session
# value from the login redirect -- never persisted, by design, once spent.
# A first version called /customerdetails again using the stored, already-
# exchanged session key in that field, which is structurally the wrong value
# and got a live "Invalid session" back. There is nothing to gain by trying
# a shape that can never succeed: exchange_session already captured
# broker_client_id (idirect_userid) the moment the exchange succeeded, so
# get_profile now just reports that instead of calling out again.


async def test_get_profile_reports_the_stored_user_id_without_a_network_call(monkeypatch):
    a = adapter(monkeypatch, user_id="ICICI123")
    profile = await a.get_profile()
    assert profile.broker_client_id == "ICICI123"


async def test_get_profile_without_a_session_is_refused(monkeypatch):
    a = adapter(monkeypatch, session=None, user_id=None)
    with pytest.raises(SessionExpiredError, match="login flow"):
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


# ── get_funds reads the field that is actually free cash ────────────────
# Regression, found live against a real account: total_bank_balance mirrors
# allocated_equity (money already committed to equity trading), not free
# cash -- confirmed by comparing against the operator's real available
# balance in the ICICI Direct app, since Breeze's docs don't define either
# field explicitly. unallocated_balance is the one that matched.


async def test_get_funds_reads_unallocated_balance_not_total_bank_balance(monkeypatch):
    a = adapter(monkeypatch)

    async def fake_request(self, method, path):
        # A real payload, captured from an actual account: unallocated_balance
        # (the true free cash) arrives as a string while every other numeric
        # field here is a float, which the fix has to tolerate.
        return {
            "total_bank_balance": 1956.83,
            "allocated_equity": 1956.83,
            "unallocated_balance": "69857.02",
        }

    monkeypatch.setattr(BreezeAdapter, "_request", fake_request)
    funds = await a.get_funds()
    assert funds.available_cash == Decimal("69857.02")


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


# ── response parsing: fields that are not what they look like ────────
# Every bug in this block was live against a real account or confirmed
# against Breeze's docs. The shared shape: a plausible field name that
# means something else, or an enum cast on a broker string inside a list
# comprehension, where one bad row takes down the whole fetch.


async def test_holdings_report_the_sellable_quantity_not_the_total(monkeypatch):
    """quantity is the total on record; demat_avail_quantity is what can be
    sold today. The docs' own sample has quantity=1 against
    demat_avail_quantity=0 -- pledged stock counted as sellable."""
    a = adapter(monkeypatch)

    async def fake_request(self, method, path, body=None):
        return [{
            "stock_code": "RELIND",
            "quantity": "10",
            "demat_avail_quantity": "4",
            "blocked_quantity": "6",
        }]

    monkeypatch.setattr(BreezeAdapter, "_request", fake_request)
    holdings = await a.get_holdings()
    assert holdings[0].quantity == 4, "sizing a sell off the total oversells"
    assert holdings[0].total_quantity == 10


async def test_holdings_do_not_invent_a_cost_basis(monkeypatch):
    """/dematholdings carries no average price at all. Reading one yielded
    Decimal(0) for every row -- not 'unknown' but a claim the stock was
    free, making unrealized P&L the full notional. Confirmed live: all 15
    holdings on a real account came back this way."""
    a = adapter(monkeypatch)

    async def fake_request(self, method, path, body=None):
        return [{"stock_code": "RELIND", "quantity": "10", "demat_avail_quantity": "10"}]

    monkeypatch.setattr(BreezeAdapter, "_request", fake_request)
    assert (await a.get_holdings())[0].average_price is None


async def test_a_holding_with_an_unparsable_quantity_does_not_break_the_fetch(monkeypatch):
    a = adapter(monkeypatch)

    async def fake_request(self, method, path, body=None):
        return [
            {"stock_code": "A", "demat_avail_quantity": "-"},
            {"stock_code": "B", "demat_avail_quantity": "5.0"},
            {"stock_code": "C", "demat_avail_quantity": "3"},
        ]

    monkeypatch.setattr(BreezeAdapter, "_request", fake_request)
    holdings = await a.get_holdings()
    assert [h.quantity for h in holdings] == [0, 5, 3]


async def test_positions_carry_breezes_own_product_not_a_hardcoded_mis(monkeypatch):
    """product was hardcoded MIS for every row. A delivery holding labelled
    intraday is one square-off-before-close away from being liquidated."""
    a = adapter(monkeypatch)

    async def fake_request(self, method, path, body=None):
        return [
            {"stock_code": "RELIND", "exchange_code": "NSE",
             "product_type": "cash", "quantity": "10", "average_price": "100"},
            {"stock_code": "NIFTY", "exchange_code": "NFO",
             "product_type": "Futures", "quantity": "50", "average_price": "200"},
        ]

    monkeypatch.setattr(BreezeAdapter, "_request", fake_request)
    positions = await a.get_positions()
    assert positions[0].product is ProductType.CNC
    assert positions[1].product is ProductType.NRML


async def test_one_unknown_exchange_does_not_hide_every_other_position(monkeypatch):
    a = adapter(monkeypatch)

    async def fake_request(self, method, path, body=None):
        # "NSE_IDX" is not an Exchange member. Breeze's segment strings are
        # not a closed set, and an unrecognised one used to raise ValueError
        # inside the comprehension.
        return [
            {"stock_code": "A", "exchange_code": "NSE_IDX", "quantity": "1", "average_price": "1"},
            {"stock_code": "B", "exchange_code": "NSE", "quantity": "2", "average_price": "2"},
        ]

    monkeypatch.setattr(BreezeAdapter, "_request", fake_request)
    positions = await a.get_positions()
    assert [p.symbol for p in positions] == ["B"]


async def test_a_resting_stoploss_does_not_take_down_the_order_book(monkeypatch):
    """Breeze returns order_type 'Stoploss'. OrderType('STOPLOSS') raises --
    one resting stop-loss order blinded the platform to every other order."""
    a = adapter(monkeypatch)

    async def fake_request(self, method, path, body=None):
        return [
            {"order_id": "1", "stock_code": "A", "exchange_code": "NSE",
             "action": "Buy", "order_type": "Stoploss", "quantity": "5", "status": "Ordered"},
            {"order_id": "2", "stock_code": "B", "exchange_code": "NSE",
             "action": "Sell", "order_type": "Limit", "quantity": "3", "status": "Ordered"},
        ]

    monkeypatch.setattr(BreezeAdapter, "_request", fake_request)
    orders = await a.get_orders()
    assert [o.broker_order_id for o in orders] == ["1", "2"]
    assert orders[0].order_type is OrderType.SL
    assert orders[1].order_type is OrderType.LIMIT


def test_a_stop_loss_market_order_is_refused_rather_than_sent_as_a_limit(monkeypatch):
    """SL and SL_M both mapped to Breeze's single "stoploss" type, which takes
    a trigger AND a limit price -- so a stop-loss MARKET went out as a
    stop-loss LIMIT, the same silent substitution this adapter already
    refuses for plain market orders.

    OrderRequest requires a trigger price on SL_M, so the order is built
    validly here: the refusal under test is the adapter's, not the model's.
    """
    with pytest.raises(FeatureNotSupportedError, match="stop-loss"):
        adapter(monkeypatch)._order_body(
            order(
                order_type=OrderType.SL_M,
                price=Decimal("100"),
                trigger_price=Decimal("99"),
            ),
            "cli-1",
        )
