"""Groww adapter response parsing.

Like the Zerodha adapter, this had no tests and has never run against a real
account. Its trading methods refuse outright; what these cover is the read
path -- the part that is reachable today and therefore the part that can
mislead an operator today.

No network: _request is always replaced, except where the envelope handling
itself is under test.
"""

from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app.adapters.base import BrokerError, SessionExpiredError
from app.adapters.groww.adapter import GrowwAdapter
from app.core.config import BrokerEnvCredentials
from app.domain.enums import OrderStatus


def adapter(monkeypatch, *, env_token="env-token", stored=None):
    monkeypatch.setenv("GROWW_MAIN_ACCESS_TOKEN", env_token)
    account = SimpleNamespace(
        id="acct", broker="groww", credential_ref="GROWW_MAIN",
        session_token_enc=stored, broker_client_id="", environment="paper",
    )
    return GrowwAdapter(account, BrokerEnvCredentials("GROWW_MAIN"))


def patch_response(monkeypatch, *, status=200, payload=None, text=None):
    """Replace the HTTP call, and the throttle that precedes it.

    _request paces against Redis before it sends anything. These tests are
    about the response envelope, and a real Redis is not part of that -- but
    the throttle is deliberately NOT bypassed in the adapter itself, so it has
    to be stubbed here rather than made conditional in production code.
    """
    import app.adapters.groww.adapter as groww_module

    class NoThrottle:
        async def acquire(self, *a, **kw):
            return None

    monkeypatch.setattr(groww_module, "groww_limiter", lambda *a, **kw: NoThrottle())
    monkeypatch.setattr(groww_module, "get_redis", lambda: None)

    class FakeResponse:
        status_code = status

        def json(self):
            if text is not None:
                raise ValueError("not json")
            return payload

    async def fake(self, method, url, **kwargs):
        return FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "request", fake)


# ── token precedence ─────────────────────────────────────────────────


def test_a_token_obtained_through_the_app_beats_the_bootstrap_env_var(monkeypatch):
    """Groww tokens expire daily. When the env var won unconditionally, a
    stale bootstrap token was permanent: re-authenticating wrote
    session_token_enc, nothing read it, and every reconnect reported success
    while every call 401'd."""
    from app.core.security import encrypt_secret

    a = adapter(monkeypatch, stored=encrypt_secret("fresh-token"))
    assert a._token() == "fresh-token"


def test_the_env_var_is_still_the_bootstrap_when_nothing_is_stored(monkeypatch):
    assert adapter(monkeypatch)._token() == "env-token"


def test_no_token_anywhere_asks_for_a_login_not_a_broker_error(monkeypatch):
    a = adapter(monkeypatch, env_token="")
    with pytest.raises(SessionExpiredError):
        a._token()


# ── the response envelope ────────────────────────────────────────────


async def test_a_failure_envelope_is_an_error_not_an_empty_portfolio(monkeypatch):
    """Groww returns {"status": "FAILURE"} with HTTP 200. Nothing inspected
    it, so the error body fell through `body.get("payload", body)` and
    callers read `.get("holdings", [])` off it -- an error rendered as "you
    hold nothing", which is the worst possible default for a portfolio."""
    a = adapter(monkeypatch)
    patch_response(monkeypatch, payload={
        "status": "FAILURE",
        "error": {"code": "GA001", "message": "Invalid segment"},
    })
    with pytest.raises(BrokerError, match="Invalid segment"):
        await a._request("GET", "/holdings/user")


async def test_an_html_error_page_is_a_broker_error_not_an_unhandled_500(monkeypatch):
    """A 502 from the edge is HTML; json() raises JSONDecodeError, which is
    not a BrokerError and so escaped every handler in the service layer as
    an unhandled 500 rather than a broker failure the account status could
    record."""
    a = adapter(monkeypatch)
    patch_response(monkeypatch, status=502, text="<html>bad gateway</html>")
    with pytest.raises(BrokerError) as excinfo:
        await a._request("GET", "/holdings/user")
    assert excinfo.value.retryable is True


async def test_a_shape_we_do_not_recognise_is_refused_rather_than_read_as_empty(monkeypatch):
    a = adapter(monkeypatch)
    patch_response(monkeypatch, payload={"unexpected": "shape"})
    with pytest.raises(BrokerError, match="Unexpected Groww response shape"):
        await a._request("GET", "/holdings/user")


async def test_a_401_asks_for_a_login(monkeypatch):
    a = adapter(monkeypatch)
    patch_response(monkeypatch, status=401, payload={"error": "expired"})
    with pytest.raises(SessionExpiredError):
        await a._request("GET", "/holdings/user")


# ── holdings and positions ───────────────────────────────────────────


async def test_sellable_quantity_excludes_pledged_and_locked_stock(monkeypatch):
    """Sizing a sell off the total counts pledged and locked stock as
    sellable -- the same trap as Breeze's dematholdings. The total is kept
    separately so the portfolio view can still show what is owned."""
    a = adapter(monkeypatch)

    async def fake(self, method, path, **kwargs):
        return {"holdings": [{
            "trading_symbol": "RELIANCE", "quantity": 100,
            "demat_free_quantity": 60, "pledge_quantity": 40,
            "average_price": "2800",
        }]}

    monkeypatch.setattr(GrowwAdapter, "_request", fake)
    holding = (await a.get_holdings())[0]
    assert holding.quantity == 60
    assert holding.total_quantity == 100


async def test_position_cost_basis_reads_the_field_groww_actually_returns(monkeypatch):
    """`average_price` is not a field Groww returns on a position; its price
    fields are credit_price, debit_price, net_price and the carry_forward_*
    pair. Reading the absent one gave every position a cost basis of zero,
    which makes unrealized P&L the entire notional."""
    a = adapter(monkeypatch)

    async def fake(self, method, path, **kwargs):
        return {"positions": [{
            "trading_symbol": "RELIANCE", "exchange": "NSE", "product": "CNC",
            "quantity": 10, "net_price": "2800",
        }]}

    monkeypatch.setattr(GrowwAdapter, "_request", fake)
    assert (await a.get_positions())[0].average_price == Decimal("2800")


async def test_one_unmappable_position_does_not_hide_the_rest(monkeypatch):
    a = adapter(monkeypatch)

    async def fake(self, method, path, **kwargs):
        return {"positions": [
            {"trading_symbol": "A", "exchange": "NSE", "product": "WEIRD",
             "quantity": 1, "net_price": "1"},
            {"trading_symbol": "B", "exchange": "NSE", "product": "CNC",
             "quantity": 2, "net_price": "2"},
        ]}

    monkeypatch.setattr(GrowwAdapter, "_request", fake)
    assert [p.symbol for p in await a.get_positions()] == ["B"]


# ── order status ─────────────────────────────────────────────────────


async def test_an_unknown_status_is_skipped_not_reported_as_a_working_order(monkeypatch):
    """Defaulting an unknown status to OPEN claims a live working order
    exists. For a terminal state Groww added since the map was written, that
    is a phantom order the platform never retries and may double-count
    exposure against."""
    a = adapter(monkeypatch)

    async def fake(self, method, path, **kwargs):
        return {"order_list": [
            {"groww_order_id": "1", "trading_symbol": "A", "exchange": "NSE",
             "transaction_type": "BUY", "order_type": "LIMIT", "product": "CNC",
             "quantity": 1, "order_status": "SOMETHING_NEW"},
            {"groww_order_id": "2", "trading_symbol": "B", "exchange": "NSE",
             "transaction_type": "BUY", "order_type": "LIMIT", "product": "CNC",
             "quantity": 1, "order_status": "EXECUTED"},
        ]}

    monkeypatch.setattr(GrowwAdapter, "_request", fake)
    orders = await a.get_orders()
    assert [o.broker_order_id for o in orders] == ["2"]
    assert orders[0].status is OrderStatus.FILLED


async def test_the_terminal_states_groww_documents_map_to_terminal_states(monkeypatch):
    """FAILED and DELIVERY_AWAITED were both missing from the map; without
    them a rejected order and a settled delivery both read as OPEN."""
    from app.adapters.groww.adapter import _STATUS_MAP

    assert _STATUS_MAP["FAILED"] is OrderStatus.FAILED
    assert _STATUS_MAP["DELIVERY_AWAITED"] is OrderStatus.FILLED
    assert _STATUS_MAP["TRIGGER_PENDING"] is OrderStatus.OPEN


# ── funds ────────────────────────────────────────────────────────────


async def test_missing_cash_reports_zero_rather_than_inventing_headroom(monkeypatch):
    """The old fallback was `net_margin` -- wrong twice: the real key is
    net_margin_used, so it never fired, and margin USED is the opposite of
    cash available. Had the key been right, deployed margin would have been
    reported as free money."""
    a = adapter(monkeypatch)

    async def fake(self, method, path, **kwargs):
        return {"net_margin_used": "50000"}

    monkeypatch.setattr(GrowwAdapter, "_request", fake)
    assert (await a.get_funds()).available_cash == Decimal("0")


# ── rate limiting ────────────────────────────────────────────────────


async def test_every_call_is_paced_before_it_is_sent(monkeypatch):
    """Groww was the one adapter with no rate limiting at all -- not a loose
    limit, none. The limiter infrastructure existed and was simply never
    wired in, so a read loop polling positions ran as fast as the event loop
    allowed.

    This asserts the throttle is acquired BEFORE the request goes out, not
    merely that a limiter object exists: a limiter acquired afterwards paces
    nothing.
    """
    import app.adapters.groww.adapter as groww_module

    events: list[str] = []

    class RecordingThrottle:
        async def acquire(self, *a, **kw):
            events.append("throttle")

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"payload": {}}

    async def fake_send(self, method, url, **kwargs):
        events.append("request")
        return FakeResponse()

    monkeypatch.setattr(groww_module, "get_redis", lambda: None)
    monkeypatch.setattr(
        groww_module, "groww_limiter", lambda *a, **kw: RecordingThrottle()
    )
    monkeypatch.setattr(httpx.AsyncClient, "request", fake_send)

    await adapter(monkeypatch)._request("GET", "/holdings/user")
    assert events == ["throttle", "request"]


def test_the_bucket_is_keyed_per_credential_not_per_account(monkeypatch):
    """Two accounts sharing one set of Groww credentials share whatever
    budget Groww applies to it; keying per account id would give each its
    own bucket and together they would exceed the real limit."""
    assert adapter(monkeypatch)._throttle_key() == "GROWW_MAIN"
