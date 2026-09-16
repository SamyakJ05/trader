"""Breeze F&O orders, order-book parsing and contract identity.

Formats here are taken from ICICI's published REST reference and from a
downloaded security master, not from their SDK's README -- which contradicts
itself on the one field that matters most. Its streaming examples use
"13-Feb-2025" while the documented POST /order specifies ISO 8601 and its own
examples send "2024-09-12T06:00:00.000Z". Following the README would have had
every F&O order rejected, so the expected values below are pinned to the
authoritative source.
"""

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.adapters.base import FeatureNotSupportedError
from app.adapters.icici_breeze.adapter import BreezeAdapter, _map_status
from app.core.config import BrokerEnvCredentials
from app.domain.enums import (
    Exchange,
    OptionRight,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
)
from app.domain.models import OrderRequest


def adapter():
    account = SimpleNamespace(
        id=uuid.uuid4(), broker="icici_breeze", credential_ref="ICICI_MAIN",
        session_token_enc=None, broker_client_id="X", environment="live",
    )
    return BreezeAdapter(account, BrokerEnvCredentials("ICICI_MAIN"))


def option(**kw):
    base = dict(
        symbol="NIFTY", exchange=Exchange.NFO, side=OrderSide.BUY,
        order_type=OrderType.LIMIT, product=ProductType.NRML, quantity=25,
        price=Decimal("1"), expiry=date(2024, 9, 12), strike=Decimal("25000"),
        right=OptionRight.CALL,
    )
    base.update(kw)
    return OrderRequest(**base)


# ── the payload ICICI documents ──────────────────────────────────────


def test_an_option_order_matches_icicis_own_documented_example():
    """Pinned field for field against the example in ICICI's REST reference
    for POST /order. These three fields were omitted entirely, so no F&O
    order could be placed at all despite NFO being advertised."""
    body = adapter()._order_body(option(), "cli-1")
    assert body["expiry_date"] == "2024-09-12T06:00:00.000Z"
    assert body["right"] == "call"
    assert body["strike_price"] == "25000"
    assert body["product"] == "options"


def test_a_future_sends_others_and_a_zero_strike():
    """ICICI's futures example sends right "others" and strike "0". An empty
    string in either is rejected, so neither may be left blank."""
    body = adapter()._order_body(
        option(symbol="CNXBAN", strike=None, right=OptionRight.OTHERS), "cli-2"
    )
    assert body["right"] == "others"
    assert body["strike_price"] == "0"
    assert body["product"] == "futures"


def test_product_follows_the_contract_not_the_product_type():
    """Breeze names futures and options as different products, and our NRML
    maps to both. Sending an option as "futures" would be a different
    instrument."""
    a = adapter()
    assert a._order_body(option(), "c")["product"] == "options"
    assert (
        a._order_body(option(strike=None, right=OptionRight.OTHERS), "c")["product"]
        == "futures"
    )


def test_a_cash_order_leaves_the_derivatives_fields_empty():
    """Breeze documents the three as optional for product cash, and an
    equity order has no contract to name."""
    body = adapter()._order_body(
        OrderRequest(
            symbol="RELIND", exchange=Exchange.NSE, side=OrderSide.BUY,
            order_type=OrderType.LIMIT, product=ProductType.CNC, quantity=1,
            price=Decimal("2800"),
        ),
        "cli-3",
    )
    assert body["expiry_date"] == ""
    assert body["right"] == ""
    assert body["strike_price"] == ""


def test_an_nfo_order_without_an_expiry_is_refused():
    """A symbol alone does not name a contract -- Breeze lists 3,350 NIFTY
    contracts under that one code. Sending one without an expiry would be a
    broker rejection with a vaguer message."""
    request = OrderRequest(
        symbol="NIFTY", exchange=Exchange.NFO, side=OrderSide.BUY,
        order_type=OrderType.LIMIT, product=ProductType.NRML, quantity=25,
        price=Decimal("1"),
    )
    with pytest.raises(FeatureNotSupportedError, match="expiry"):
        adapter()._order_body(request, "cli-4")


async def test_modify_also_carries_the_contract():
    """PUT /order marks expiry_date, right and strike_price mandatory too.
    Without them an F&O order could not be amended -- and repricing a resting
    option is the main reason to amend one."""
    a = adapter()
    captured = {}

    async def fake_request(method, path, body=None):
        captured.update(body or {})
        return {}

    a._request = fake_request
    await a.modify_order("B-1", option())
    assert captured["expiry_date"] == "2024-09-12T06:00:00.000Z"
    assert captured["right"] == "call"
    assert captured["strike_price"] == "25000"


# ── the order book ───────────────────────────────────────────────────


async def test_filled_quantity_is_derived_because_breeze_does_not_report_it():
    """Breeze's order book has NO filled_quantity field. Its documented
    response carries quantity, pending_quantity and cancelled_quantity, and
    nothing populated filled_quantity at all -- so the reconciler, which books
    whatever that field reports, would have found every order permanently
    unfilled and booked no fill, ever."""
    a = adapter()

    async def fake_request(method, path, body=None):
        return [{
            "order_id": "20250205N300001234", "exchange_code": "NSE",
            "stock_code": "ITC", "action": "Buy", "order_type": "Limit",
            "product_type": "Cash", "quantity": "10", "pending_quantity": "4",
            "cancelled_quantity": "0", "average_price": "420.50",
            "price": "420.00", "status": "Partially Executed",
        }]

    a._request = fake_request
    orders = await a.get_orders()
    assert orders[0].filled_quantity == 6
    assert orders[0].average_fill_price == Decimal("420.50")
    assert orders[0].status is OrderStatus.PARTIALLY_FILLED


async def test_an_unfilled_order_reports_no_average_price_rather_than_zero():
    """Breeze sends average_price '0' before anything fills. Passing zero
    through would let a consumer read "not yet filled" as "filled for free";
    the reconciler refuses to book a fill without a price for that reason."""
    a = adapter()

    async def fake_request(method, path, body=None):
        return [{
            "order_id": "1", "exchange_code": "NSE", "stock_code": "ITC",
            "action": "Buy", "order_type": "Limit", "product_type": "Cash",
            "quantity": "1", "pending_quantity": "1", "cancelled_quantity": "0",
            "average_price": "0", "status": "Ordered",
        }]

    a._request = fake_request
    orders = await a.get_orders()
    assert orders[0].average_fill_price is None
    assert orders[0].filled_quantity == 0


# ── status vocabulary ────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("Ordered", OrderStatus.OPEN),
    ("Queued", OrderStatus.SUBMITTED),
    ("Requested", OrderStatus.SUBMITTED),
    ("Executed", OrderStatus.FILLED),
    ("Partially Executed", OrderStatus.PARTIALLY_FILLED),
    ("Cancelled", OrderStatus.CANCELLED),
    ("Rejected", OrderStatus.REJECTED),
    # The five that were missing. An unmapped status used to read as OPEN.
    ("Expired", OrderStatus.CANCELLED),
    ("Freezed", OrderStatus.REJECTED),
    ("Partially Executed And Cancelled", OrderStatus.CANCELLED),
    ("Partially Executed And Expired", OrderStatus.CANCELLED),
    # Casing and spacing vary in practice.
    ("executed", OrderStatus.FILLED),
    ("  Partially   Executed ", OrderStatus.PARTIALLY_FILLED),
])
def test_every_status_breeze_documents_is_mapped(raw, expected):
    """A terminal order reported as OPEN is polled forever by the reconciler
    and counted as live exposure the account does not have."""
    assert _map_status(raw) is expected


def test_an_unknown_status_is_not_guessed_at():
    assert _map_status("Something Breeze Added Later") is None
    assert _map_status("") is None


# ── cancelling ───────────────────────────────────────────────────────


async def test_cancel_sends_the_orders_own_exchange():
    """exchange_code is required on DELETE /order and an order id does not
    carry it. This hardcoded NSE, so an NFO order could not be cancelled at
    all -- the case where cancelling matters most, since an option left open
    through expiry settles against you."""
    a = adapter()
    captured = {}

    async def fake_request(method, path, body=None):
        captured.update(body or {})
        return {}

    a._request = fake_request
    await a.cancel_order("B-1", Exchange.NFO)
    assert captured["exchange_code"] == "NFO"


async def test_cancel_without_an_exchange_still_defaults_to_nse():
    """The old behaviour, kept for a caller that has no exchange to give --
    correct for the cash segment that was all this adapter could place."""
    a = adapter()
    captured = {}

    async def fake_request(method, path, body=None):
        captured.update(body or {})
        return {}

    a._request = fake_request
    await a.cancel_order("B-1")
    assert captured["exchange_code"] == "NSE"


# ── contract identity from the security master ───────────────────────


def test_an_option_row_yields_its_contract():
    """Column names and formats read off a downloaded FONSEScripMaster.txt:
    ExpiryDate is "29-Sep-2026" and OptionType is CE/PE/XX -- a third
    vocabulary, distinct from the order payload's call/put/others."""
    row = {
        "Token": "35078", "InstrumentName": "OPTSTK", "ShortName": "ADATRA",
        "ExpiryDate": "27-Oct-2026", "StrikePrice": "840", "OptionType": "CE",
        "LotSize": "675", "TickSize": "0.05", "CompanyName": "Adani",
    }
    instrument = adapter()._instrument_from_row(row, "NFO")
    assert instrument.expiry == date(2026, 10, 27)
    assert instrument.strike == Decimal("840")
    assert instrument.option_right is OptionRight.CALL


def test_a_future_row_has_no_strike():
    """Its master row carries strike 0 and right XX. Stored as None rather
    than zero so "no strike" is not confused with a zero strike."""
    row = {
        "Token": "68865", "InstrumentName": "FUTSTK", "ShortName": "VEDLIM",
        "ExpiryDate": "29-Sep-2026", "StrikePrice": "0", "OptionType": "XX",
        "LotSize": "1150", "TickSize": "5",
    }
    instrument = adapter()._instrument_from_row(row, "NFO")
    assert instrument.strike is None
    assert instrument.option_right is OptionRight.OTHERS


def test_an_equity_row_carries_no_contract_identity():
    row = {
        "Token": "2885", "ShortName": "RELIND", "Series": "EQ",
        "CompanyName": "RELIANCE INDUSTRIES", "Symbol": "RELIANCE",
        "LotSize": "1", "ticksize": "0.05",
    }
    instrument = adapter()._instrument_from_row(row, "NSE")
    assert instrument.expiry is None
    assert instrument.strike is None
    assert instrument.option_right is None
