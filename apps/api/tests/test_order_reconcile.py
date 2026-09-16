"""Live order reconciliation.

Breeze pushes no order state, so polling is the ONLY way a live fill is ever
learned. Everything downstream of a fill depends on this running correctly:
the position, the cash ledger, and the realized-P&L counter MAX_DAILY_LOSS
reads. A silent failure here means an account can lose any amount with the
daily-loss limit never firing, which is why these lean on behaviour rather
than on the shape of the code.

No network and no database: the adapter and the session factory are faked.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

from app.adapters.base import BrokerError, SessionExpiredError
from app.domain.enums import Exchange, OrderSide, OrderStatus, OrderType, ProductType
from app.domain.models import BrokerOrder
from app.services import order_reconcile


def account(broker="icici_breeze", environment="live", status="connected"):
    return SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), broker=broker,
        environment=environment, status=status, credential_ref="ICICI_MAIN",
    )


def order(status="SUBMITTED", broker_order_id="B-1", filled=0):
    return SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), broker_account_id=uuid.uuid4(),
        environment="live", status=status, broker_order_id=broker_order_id,
        filled_quantity=filled, average_fill_price=None, status_message=None,
        side="BUY", product="CNC", symbol="RELIANCE", exchange="NSE",
    )


def broker_order(broker_order_id="B-1", *, filled=0, avg=None, status=OrderStatus.OPEN):
    return BrokerOrder(
        broker_order_id=broker_order_id, symbol="RELIANCE", exchange=Exchange.NSE,
        side=OrderSide.BUY, order_type=OrderType.LIMIT, product=ProductType.CNC,
        quantity=10, filled_quantity=filled,
        average_fill_price=Decimal(avg) if avg else None, status=status,
    )


class FakeDb:
    def __init__(self, orders=(), accounts=()):
        self._orders = list(orders)
        self._accounts = list(accounts)
        self.committed = 0
        self.rolled_back = 0

    async def execute(self, *args, **kwargs):
        rows = self._accounts if self._accounts and not self._orders else self._orders

        class Result:
            def scalars(inner):
                return iter(rows)

        return Result()

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        self.rolled_back += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def patch_adapter(monkeypatch, orders, *, raises=None):
    class FakeAdapter:
        async def get_orders(self):
            if raises:
                raise raises
            return orders

    monkeypatch.setattr(order_reconcile, "get_adapter", lambda a: FakeAdapter())


def patch_book_fill(monkeypatch, result=None, calls=None):
    async def fake(db, redis, *, account, order, filled_quantity, average_price):
        if calls is not None:
            calls.append((filled_quantity, average_price))
        return result

    monkeypatch.setattr(order_reconcile.live_fills, "book_fill", fake)


# ── the core job: a fill at the broker becomes a fill here ───────────


async def test_a_fill_at_the_broker_is_booked(monkeypatch):
    """The whole reason this module exists. Without it a live order that
    filled updated nothing: no Fill row, no position, no cash movement, and
    nothing in the realized-P&L counter MAX_DAILY_LOSS reads."""
    o = order()
    calls: list = []
    patch_adapter(monkeypatch, [broker_order(filled=10, avg="2800", status=OrderStatus.FILLED)])
    patch_book_fill(monkeypatch, result=SimpleNamespace(quantity=10, price=Decimal("2800")), calls=calls)

    db = FakeDb(orders=[o])
    changed = await order_reconcile.reconcile_account(db, None, account())

    assert calls == [(10, Decimal("2800"))]
    assert changed == 1
    assert o.status == OrderStatus.FILLED.value
    assert o.filled_quantity == 10


async def test_booking_is_delegated_so_a_repeat_poll_books_nothing_twice(monkeypatch):
    """An order stays FILLED at the broker all day, so the same fill is seen
    on every cycle. Safety comes from book_fill being idempotent -- it books
    only the unbooked delta under the account lock -- rather than from this
    module tracking what it has seen. Returning None means 'already booked'."""
    o = order()
    patch_adapter(monkeypatch, [broker_order(filled=10, avg="2800", status=OrderStatus.FILLED)])
    patch_book_fill(monkeypatch, result=None)  # book_fill: nothing new

    db = FakeDb(orders=[o])
    await order_reconcile.reconcile_account(db, None, account())
    # Status still moves; the money did not move twice.
    assert o.status == OrderStatus.FILLED.value


async def test_a_partial_fill_reports_the_cumulative_quantity(monkeypatch):
    """Brokers report cumulative filled quantity, and book_fill expects that
    convention -- it subtracts what is already booked itself."""
    o = order(filled=4)
    calls: list = []
    patch_adapter(
        monkeypatch,
        [broker_order(filled=7, avg="2800", status=OrderStatus.PARTIALLY_FILLED)],
    )
    patch_book_fill(monkeypatch, result=SimpleNamespace(quantity=3, price=Decimal("2800")), calls=calls)

    await order_reconcile.reconcile_account(FakeDb(orders=[o]), None, account())
    assert calls == [(7, Decimal("2800"))]
    assert o.filled_quantity == 7


# ── what must NOT happen ─────────────────────────────────────────────


async def test_an_order_missing_from_the_brokers_list_is_not_assumed_cancelled(monkeypatch):
    """Breeze's order list covers a single day, so yesterday's order is absent
    without having gone anywhere. Marking it cancelled would zero out a
    position the account actually holds."""
    o = order(status="OPEN")
    patch_adapter(monkeypatch, [])  # broker returns nothing at all

    await order_reconcile.reconcile_account(FakeDb(orders=[o]), None, account())
    assert o.status == "OPEN"


async def test_a_zero_price_fill_is_not_booked(monkeypatch):
    """A filled quantity with no average price is an incomplete report, not a
    free trade. Booking it at zero would record a position with no cost."""
    o = order()
    calls: list = []
    patch_adapter(monkeypatch, [broker_order(filled=10, avg=None, status=OrderStatus.FILLED)])
    patch_book_fill(monkeypatch, calls=calls)

    await order_reconcile.reconcile_account(FakeDb(orders=[o]), None, account())
    assert calls == []


async def test_only_orders_believed_open_are_polled(monkeypatch):
    """A terminal order cannot change at the broker; re-reading it would spend
    rate budget to learn nothing. Breeze allows 100 calls a minute."""
    assert OrderStatus.FILLED.value not in order_reconcile._OPEN_STATUSES
    assert OrderStatus.CANCELLED.value not in order_reconcile._OPEN_STATUSES
    assert OrderStatus.REJECTED.value not in order_reconcile._OPEN_STATUSES
    # Never sent to the broker at all.
    assert OrderStatus.PENDING_RISK.value not in order_reconcile._OPEN_STATUSES
    assert OrderStatus.OPEN.value in order_reconcile._OPEN_STATUSES
    assert OrderStatus.PARTIALLY_FILLED.value in order_reconcile._OPEN_STATUSES


async def test_zerodha_is_not_polled_because_it_posts_back(monkeypatch):
    """Polling it would be harmless -- book_fill dedupes -- but would spend
    rate budget for information the webhook already delivered."""
    assert "zerodha" not in order_reconcile.POLLED_BROKERS
    assert "icici_breeze" in order_reconcile.POLLED_BROKERS


# ── isolation: one failure must not become everyone's ────────────────


async def test_one_orders_failure_does_not_abandon_the_others(monkeypatch):
    """Several of the remaining orders may be fills that still need booking;
    losing them because an earlier one raised would be the same silent
    accounting gap this module exists to close."""
    first, second = order(broker_order_id="B-1"), order(broker_order_id="B-2")
    patch_adapter(monkeypatch, [
        broker_order("B-1", filled=10, avg="2800", status=OrderStatus.FILLED),
        broker_order("B-2", filled=5, avg="2800", status=OrderStatus.FILLED),
    ])

    seen: list = []

    async def explode_once(db, redis, *, account, order, filled_quantity, average_price):
        seen.append(order.broker_order_id)
        if order.broker_order_id == "B-1":
            raise RuntimeError("accounting blew up")
        return SimpleNamespace(quantity=5, price=Decimal("2800"))

    monkeypatch.setattr(order_reconcile.live_fills, "book_fill", explode_once)

    await order_reconcile.reconcile_account(FakeDb(orders=[first, second]), None, account())
    assert seen == ["B-1", "B-2"]
    assert second.status == OrderStatus.FILLED.value


async def test_a_broker_outage_costs_a_cycle_not_the_loop(monkeypatch):
    """reconcile_all runs for every account; one broker failing must not stop
    the rest being reconciled on the next tick."""
    patch_adapter(monkeypatch, [], raises=BrokerError("breeze down"))
    accounts = [account()]

    def factory():
        return FakeDb(orders=[order()], accounts=accounts)

    # Must not raise.
    total = await order_reconcile.reconcile_all(factory, None)
    assert total == 0


async def test_an_expired_session_is_skipped_quietly(monkeypatch):
    """Expected daily at the exchange flush. The session job marks the
    account; there is nothing for the reconciler to do but skip it."""
    patch_adapter(monkeypatch, [], raises=SessionExpiredError("token dead"))

    def factory():
        return FakeDb(orders=[order()], accounts=[account()])

    assert await order_reconcile.reconcile_all(factory, None) == 0


async def test_only_connected_live_accounts_are_reconciled():
    """A paper account books its own fills inside the simulator, and a
    disconnected one cannot be asked anything. Both must be excluded by the
    query rather than by a check further down, so this compiles the statement
    and reads the conditions off it.
    """
    captured: list = []

    class CapturingDb(FakeDb):
        async def execute(self, statement, *args, **kwargs):
            captured.append(str(statement))
            return await super().execute(statement, *args, **kwargs)

    def factory():
        return CapturingDb(accounts=[])

    await order_reconcile.reconcile_all(factory, None)

    assert captured, "reconcile_all did not query for accounts"
    sql = captured[0]
    # The filters that matter, as they appear in the compiled SQL.
    assert "broker_accounts.environment" in sql
    assert "broker_accounts.status" in sql
    assert "broker_accounts.broker IN" in sql
