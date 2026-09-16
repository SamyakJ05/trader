"""Exercising every broker read from the UI.

The verification playbooks required an async REPL: call get_holdings,
get_positions and get_orders by hand, then check that strategy symbols resolve
to instrument tokens. That is a reading task -- compare our numbers against
the broker's own dashboard -- repeated per broker and after every adapter
change.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

from app.adapters.base import BrokerError, FeatureNotSupportedError, SessionExpiredError
from app.domain.enums import Exchange, OrderSide, OrderStatus, OrderType, ProductType
from app.domain.models import BrokerOrder, BrokerPosition, BrokerProfile, Funds, Holding
from app.services import brokers as broker_service


def account(**kw):
    base = dict(
        id=uuid.uuid4(), user_id=uuid.uuid4(), broker="icici_breeze",
        environment="live", status="connected", read_verified_at=None,
        status_message=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class FakeDb:
    def __init__(self, symbol_rows=()):
        self._rows = list(symbol_rows)

    async def execute(self, *a, **kw):
        rows = self._rows

        class Result:
            def scalars(inner):
                return iter(rows)

        return Result()


def patch_adapter(monkeypatch, **overrides):
    """An adapter whose reads succeed unless an override raises."""

    class FakeAdapter:
        async def get_profile(self):
            if "profile" in overrides:
                raise overrides["profile"]
            return BrokerProfile(broker_client_id="CLIENT1", name="A Trader")

        async def get_funds(self):
            if "funds" in overrides:
                raise overrides["funds"]
            return Funds(available_cash=Decimal("1956.83"))

        async def get_holdings(self):
            if "holdings" in overrides:
                raise overrides["holdings"]
            return [
                Holding(symbol="RELIND", exchange=Exchange.NSE, quantity=6,
                        total_quantity=10, average_price=None)
            ]

        async def get_positions(self):
            if "positions" in overrides:
                raise overrides["positions"]
            return [
                BrokerPosition(symbol="RELIND", exchange=Exchange.NSE,
                               product=ProductType.CNC, quantity=5,
                               average_price=Decimal("2800"))
            ]

        async def get_orders(self):
            if "orders" in overrides:
                raise overrides["orders"]
            return [
                BrokerOrder(broker_order_id="B-1", symbol="RELIND",
                            exchange=Exchange.NSE, side=OrderSide.BUY,
                            order_type=OrderType.LIMIT, product=ProductType.CNC,
                            quantity=10, filled_quantity=4,
                            status=OrderStatus.PARTIALLY_FILLED)
            ]

    monkeypatch.setattr(broker_service, "get_adapter", lambda a: FakeAdapter())


def patch_tokens(monkeypatch, mapping):
    async def fake_token_map(db, *, broker, symbols, exchange="NSE"):
        return mapping

    from app.services import instruments

    monkeypatch.setattr(instruments, "token_map", fake_token_map)


# ── the reads ────────────────────────────────────────────────────────


async def test_every_read_is_exercised(monkeypatch):
    """verify covers profile and funds only; the rest required a REPL."""
    patch_adapter(monkeypatch)
    patch_tokens(monkeypatch, {})
    report = await broker_service.diagnostics(FakeDb(), account())
    assert set(report["reads"]) == {"profile", "funds", "holdings", "positions", "orders"}
    assert all(r["status"] == "ok" for r in report["reads"].values())


async def test_holdings_show_sellable_against_total(monkeypatch):
    """They differ when stock is pledged or locked, and sizing a sell off the
    total is the mistake this makes visible."""
    patch_adapter(monkeypatch)
    patch_tokens(monkeypatch, {})
    report = await broker_service.diagnostics(FakeDb(), account())
    sample = report["reads"]["holdings"]["sample"][0]
    assert sample["quantity"] == 6
    assert sample["total_quantity"] == 10


async def test_one_failing_read_leaves_the_others_visible(monkeypatch):
    """The useful signal is usually which reads differ, so a single failure
    must not collapse the report."""
    patch_adapter(monkeypatch, holdings=BrokerError("dematholdings unavailable"))
    patch_tokens(monkeypatch, {})
    report = await broker_service.diagnostics(FakeDb(), account())
    assert report["reads"]["holdings"]["status"] == "error"
    assert report["reads"]["funds"]["status"] == "ok"
    assert report["reads"]["orders"]["status"] == "ok"


async def test_an_unsupported_read_is_not_an_error(monkeypatch):
    """Groww implements no order list at all. Reporting that as a failure
    would train the operator to ignore real ones."""
    patch_adapter(monkeypatch, orders=FeatureNotSupportedError("not implemented"))
    patch_tokens(monkeypatch, {})
    report = await broker_service.diagnostics(FakeDb(), account())
    assert report["reads"]["orders"]["status"] == "unsupported"


async def test_a_lapsed_session_is_named_as_such(monkeypatch):
    """Distinct from an error: the fix is logging in again, not debugging."""
    patch_adapter(monkeypatch, profile=SessionExpiredError("token dead"))
    patch_tokens(monkeypatch, {})
    report = await broker_service.diagnostics(FakeDb(), account())
    assert report["reads"]["profile"]["status"] == "session_expired"


async def test_an_unexpected_exception_does_not_break_the_diagnostic(monkeypatch):
    """An adapter bug is exactly what this is for finding, so it must survive
    one rather than raising out."""
    patch_adapter(monkeypatch, positions=KeyError("average_price"))
    patch_tokens(monkeypatch, {})
    report = await broker_service.diagnostics(FakeDb(), account())
    assert report["reads"]["positions"]["status"] == "error"
    assert "KeyError" in report["reads"]["positions"]["detail"]


# ── the gate this must not touch ─────────────────────────────────────


async def test_diagnostics_never_stamps_verification(monkeypatch):
    """read_verified_at gates enabling live trading. A panel the operator can
    run at any time must not promote an account's verification state as a side
    effect -- /verify stays the only thing that can."""
    patch_adapter(monkeypatch)
    patch_tokens(monkeypatch, {})
    acct = account(read_verified_at=None)
    await broker_service.diagnostics(FakeDb(), acct)
    assert acct.read_verified_at is None


async def test_diagnostics_does_not_change_account_status(monkeypatch):
    """Even when a read fails. verify_read_access marks an account ERROR
    deliberately; a read-only panel must not."""
    patch_adapter(monkeypatch, funds=BrokerError("broker down"))
    patch_tokens(monkeypatch, {})
    acct = account(status="connected")
    await broker_service.diagnostics(FakeDb(), acct)
    assert acct.status == "connected"


# ── instrument coverage, the silent failure ──────────────────────────


async def test_symbols_without_a_token_are_named(monkeypatch):
    """The tick stream subscribes by token, so a symbol with none receives no
    prices and the runner refuses to trade it live for want of a quote.
    Nothing errors; the strategy just sits silent."""
    patch_adapter(monkeypatch)
    patch_tokens(monkeypatch, {"2885": "RELIND"})
    db = FakeDb(symbol_rows=[["RELIND", "INFTEC", "TCS"]])
    report = await broker_service.diagnostics(db, account())
    coverage = report["instrument_coverage"]
    assert coverage["symbols"] == 3
    assert coverage["resolved"] == 1
    assert coverage["missing"] == ["INFTEC", "TCS"]


async def test_an_account_with_no_strategies_reports_nothing_missing(monkeypatch):
    patch_adapter(monkeypatch)
    patch_tokens(monkeypatch, {})
    report = await broker_service.diagnostics(FakeDb(), account())
    assert report["instrument_coverage"] == {"symbols": 0, "resolved": 0, "missing": []}
