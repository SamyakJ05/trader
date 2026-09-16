"""Zerodha Kite adapter response parsing.

This adapter had no tests. It is SCAFFOLD -- never run against a real
account -- and the Breeze adapter, which was in exactly that state until it
met one, turned out to have a bug in every read method. These pin the
response-parsing decisions that a real account would otherwise be the first
thing to check.

No network: _request is always replaced.
"""

from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app.adapters.base import BrokerError, SessionExpiredError
from app.adapters.zerodha.adapter import ZerodhaAdapter
from app.core.config import BrokerEnvCredentials
from app.domain.enums import Exchange, OrderSide, OrderType, ProductType, Validity
from app.domain.models import OrderRequest


def account(session="plain:tok"):
    return SimpleNamespace(
        id="acct", broker="zerodha", credential_ref="ZERODHA_MAIN",
        session_token_enc=session, broker_client_id="AB1234", environment="paper",
    )


def adapter(monkeypatch, **kw):
    monkeypatch.setenv("ZERODHA_MAIN_API_KEY", "key")
    monkeypatch.setenv("ZERODHA_MAIN_API_SECRET", "secret")
    return ZerodhaAdapter(account(**kw), BrokerEnvCredentials("ZERODHA_MAIN"))


def patch_request(monkeypatch, payload):
    async def fake(self, method, path, **kwargs):
        return payload

    monkeypatch.setattr(ZerodhaAdapter, "_request", fake)


# ── funds: the total-vs-available trap ───────────────────────────────


async def test_available_cash_is_net_not_raw_cash(monkeypatch):
    """Kite documents available.cash as the RAW cash balance: it excludes
    collateral and does not subtract utilised debits. `net` is the segment's
    net cash available for trading -- the figure Kite's own dashboard shows.
    Reading available.cash under-reports a pledged account and over-reports
    whenever anything is deployed."""
    a = adapter(monkeypatch)
    patch_request(monkeypatch, {
        "net": "45000",
        "available": {"cash": "10000", "collateral": "40000"},
        "utilised": {"debits": "5000"},
    })
    funds = await a.get_funds()
    assert funds.available_cash == Decimal("45000")
    assert funds.margin_used == Decimal("5000")


# ── error discrimination ─────────────────────────────────────────────


async def test_a_token_exception_is_a_session_error_not_a_generic_one(monkeypatch):
    """Kite's error_type is the authoritative discriminator and was being
    discarded. An expired daily token surfacing as a generic BrokerError
    marks the account ERROR ("the broker is broken") rather than
    SESSION_EXPIRED ("log in again"), and orders drop silently for a day."""

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "status": "error",
                "error_type": "TokenException",
                "message": "Incorrect api_key or access_token",
            }

    async def fake_send(self, method, path, **kwargs):
        return FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_send)
    monkeypatch.setattr(ZerodhaAdapter, "_throttle_category", lambda self, p: "default")
    # Empty api key: the throttle keys on it, and this test is about error
    # discrimination rather than pacing.
    monkeypatch.setenv("ZERODHA_MAIN_API_KEY", "")
    a = ZerodhaAdapter(account(), BrokerEnvCredentials("ZERODHA_MAIN"))
    with pytest.raises(SessionExpiredError):
        await a._request("GET", "/user/profile")


# ── positions: intraday vs overall P&L, and unmappable rows ──────────


async def test_unrealized_pnl_is_the_overall_figure_not_the_intraday_slice(monkeypatch):
    """Kite's `unrealised` is intraday-only. For a carry-forward position it
    describes today's slice; `pnl` is the position's actual open P&L."""
    a = adapter(monkeypatch)
    patch_request(monkeypatch, {"net": [{
        "tradingsymbol": "RELIANCE", "exchange": "NSE", "product": "CNC",
        "quantity": 10, "average_price": "2800", "last_price": "2900",
        "realised": "0", "unrealised": "200", "pnl": "1000",
    }]})
    position = (await a.get_positions())[0]
    assert position.unrealized_pnl == Decimal("1000")


async def test_one_unmappable_product_does_not_hide_every_position(monkeypatch):
    """MTF is a real Kite product our ProductType cannot express. The cast
    used to raise inside a list comprehension, blinding the platform to
    every other position rather than skipping the one row."""
    a = adapter(monkeypatch)
    patch_request(monkeypatch, {"net": [
        {"tradingsymbol": "A", "exchange": "NSE", "product": "MTF",
         "quantity": 1, "average_price": "1", "last_price": "1",
         "realised": "0", "unrealised": "0", "pnl": "0"},
        {"tradingsymbol": "B", "exchange": "NSE", "product": "CNC",
         "quantity": 2, "average_price": "2", "last_price": "2",
         "realised": "0", "unrealised": "0", "pnl": "0"},
    ]})
    assert [p.symbol for p in await a.get_positions()] == ["B"]


# ── orders: the substitution that matters ────────────────────────────


def test_an_unknown_order_type_does_not_become_a_market_order(monkeypatch):
    """Kite's order_type list is not closed. Defaulting an unrecognised one
    to MARKET claims a resting limit order is a market order, in the
    platform's own model of an order that is live at the broker. LIMIT is
    the conservative reading: it describes an order that rests."""
    a = adapter(monkeypatch)
    mapped = a._map_order({
        "order_id": "1", "tradingsymbol": "RELIANCE", "exchange": "NSE",
        "transaction_type": "BUY", "order_type": "ICEBERG_SOMETHING",
        "product": "CNC", "quantity": 1, "status": "OPEN",
    })
    assert mapped.order_type is OrderType.LIMIT


def test_a_known_order_type_still_round_trips(monkeypatch):
    a = adapter(monkeypatch)
    mapped = a._map_order({
        "order_id": "1", "tradingsymbol": "RELIANCE", "exchange": "NSE",
        "transaction_type": "BUY", "order_type": "SL-M", "product": "MIS",
        "quantity": 1, "status": "OPEN",
    })
    assert mapped.order_type is OrderType.SL_M
    assert mapped.product is ProductType.MIS
    assert mapped.exchange is Exchange.NSE


# ── placement: an order whose fate is unknown ────────────────────────


async def test_an_order_without_an_id_is_refused_not_recorded(monkeypatch):
    """str(None) is "None" -- a valid-looking string. Without this guard an
    order whose outcome is unknown was recorded as SUBMITTED with a poison
    id, and every later modify/cancel targeted /orders/regular/None."""
    a = adapter(monkeypatch)
    patch_request(monkeypatch, {})
    request = OrderRequest(
        symbol="RELIANCE", exchange=Exchange.NSE, side=OrderSide.BUY,
        order_type=OrderType.LIMIT, product=ProductType.CNC, quantity=1,
        price=Decimal("2800"), validity=Validity.DAY,
    )
    with pytest.raises(BrokerError, match="without returning an id"):
        await a.place_order(request, "cli-1")
