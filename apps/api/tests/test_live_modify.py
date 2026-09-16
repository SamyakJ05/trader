"""Amending a resting live order.

This raised "Live order modification not supported yet" outright, which made
BreezeAdapter.modify_order unreachable. Repricing a limit that has stopped
being marketable is the normal way to stay in a trade; without it the only
options are to leave the order or cancel and replace, and a cancel-replace
loses queue priority and can cross in between.

The broker is the source of truth here, so the ordering matters: it is told
first and the local row updated only on acknowledgement.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.adapters.base import BrokerError
from app.domain.enums import OrderStatus
from app.services import orders as order_service


def order(environment="live", status=OrderStatus.OPEN.value, filled=0):
    return SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), broker_account_id=uuid.uuid4(),
        environment=environment, status=status, filled_quantity=filled,
        symbol="RELIANCE", exchange="NSE", side="BUY", order_type="LIMIT",
        product="CNC", validity="DAY", quantity=10, price=Decimal("2800"),
        trigger_price=None, broker_order_id="B-1", client_order_id="cli-1",
        expiry=None, strike=None, option_right=None,
    )


def account(live_enabled=True):
    return SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), broker="icici_breeze",
        environment="live", live_enabled=live_enabled, status="connected",
    )


class FakeDb:
    def __init__(self, account_row):
        self._account = account_row
        self.committed = 0
        self.added: list = []

    def add(self, obj):
        self.added.append(obj)

    async def get(self, model, pk):
        return self._account

    async def commit(self):
        self.committed += 1

    async def refresh(self, obj):
        pass


def patch_adapter(monkeypatch, *, calls=None, raises=None):
    class FakeAdapter:
        async def modify_order(self, broker_order_id, request):
            if calls is not None:
                calls.append((broker_order_id, request))
            if raises:
                raise raises
            return SimpleNamespace(broker_order_id=broker_order_id)

    monkeypatch.setattr(order_service, "get_trading_adapter", lambda a: FakeAdapter())


def allow_live(monkeypatch):
    monkeypatch.setattr(order_service, "_live_gate", lambda account: None)


async def test_a_live_modify_reaches_the_broker(monkeypatch):
    """Previously it raised before ever getting here, so the adapter's
    modify_order was dead code."""
    o = order()
    calls: list = []
    allow_live(monkeypatch)
    patch_adapter(monkeypatch, calls=calls)

    await order_service.modify_order(
        FakeDb(account()), None, user_id=o.user_id, order=o,
        price=Decimal("2850"), quantity=None,
    )

    assert len(calls) == 1
    broker_order_id, request = calls[0]
    assert broker_order_id == "B-1"
    assert request.price == Decimal("2850")
    # Unchanged fields are carried through, not dropped.
    assert request.quantity == 10
    assert request.symbol == "RELIANCE"
    assert o.price == Decimal("2850")


async def test_the_local_row_is_not_updated_when_the_broker_refuses(monkeypatch):
    """The broker is the source of truth. Updating our record first would
    leave it claiming a price the broker never accepted, and the next
    reconciliation would silently overwrite it back."""
    o = order()
    allow_live(monkeypatch)
    patch_adapter(monkeypatch, raises=BrokerError("order already filled"))

    with pytest.raises(order_service.OrderServiceError, match="already filled"):
        await order_service.modify_order(
            FakeDb(account()), None, user_id=o.user_id, order=o,
            price=Decimal("2850"), quantity=None,
        )

    assert o.price == Decimal("2800"), "local price moved despite the broker refusing"


async def test_a_live_modify_is_subject_to_the_live_gate(monkeypatch):
    """Modifying a resting order changes what may execute with real money, so
    it passes the same gate as placing one. Breeze's adapter_status is
    SCAFFOLD today, which is exactly what this stops."""
    o = order()
    monkeypatch.setattr(
        order_service, "_live_gate", lambda account: "adapter status is 'scaffold'"
    )
    calls: list = []
    patch_adapter(monkeypatch, calls=calls)

    with pytest.raises(order_service.OrderServiceError, match="scaffold"):
        await order_service.modify_order(
            FakeDb(account()), None, user_id=o.user_id, order=o,
            price=Decimal("2850"), quantity=None,
        )
    assert calls == [], "the broker was contacted despite the gate refusing"


async def test_a_quantity_below_what_is_filled_is_refused_before_the_broker(monkeypatch):
    """Incoherent at any broker, and learning it from a rejected modify would
    leave the local row and the broker disagreeing."""
    o = order(filled=7)
    allow_live(monkeypatch)
    calls: list = []
    patch_adapter(monkeypatch, calls=calls)

    with pytest.raises(order_service.OrderServiceError, match="below already-filled"):
        await order_service.modify_order(
            FakeDb(account()), None, user_id=o.user_id, order=o,
            price=None, quantity=3,
        )
    assert calls == []


async def test_another_users_order_cannot_be_modified(monkeypatch):
    o = order()
    allow_live(monkeypatch)
    calls: list = []
    patch_adapter(monkeypatch, calls=calls)

    with pytest.raises(order_service.OrderServiceError, match="does not belong"):
        await order_service.modify_order(
            FakeDb(account()), None, user_id=uuid.uuid4(), order=o,
            price=Decimal("2850"), quantity=None,
        )
    assert calls == []


async def test_a_paper_order_does_not_touch_a_broker(monkeypatch):
    """Paper amends in the simulator; routing it to a broker would send a real
    order for a simulated one."""
    o = order(environment="paper")
    calls: list = []
    patch_adapter(monkeypatch, calls=calls)

    class PaperDb(FakeDb):
        pass

    async def fake_lock(db, account_id):
        return None

    monkeypatch.setattr(order_service.paper_engine.ledger, "lock_account", fake_lock)

    await order_service.modify_order(
        PaperDb(account()), None, user_id=o.user_id, order=o,
        price=Decimal("2850"), quantity=None,
    )
    assert calls == []
    assert o.price == Decimal("2850")


async def test_an_fo_orders_contract_survives_into_the_modify(monkeypatch):
    """Breeze marks expiry, right and strike mandatory on PUT /order as well
    as POST. An order that did not remember its contract could be placed and
    then never amended -- and repricing a resting option is the main reason
    to amend one.
    """
    from datetime import date

    o = order()
    o.exchange = "NFO"
    o.product = "NRML"
    o.expiry = date(2026, 9, 29)
    o.strike = Decimal("25000")
    o.option_right = "CALL"

    calls: list = []
    allow_live(monkeypatch)
    patch_adapter(monkeypatch, calls=calls)

    await order_service.modify_order(
        FakeDb(account()), None, user_id=o.user_id, order=o,
        price=Decimal("250"), quantity=None,
    )

    _, request = calls[0]
    assert request.expiry == date(2026, 9, 29)
    assert request.strike == Decimal("25000")
    assert request.right.value == "CALL"
