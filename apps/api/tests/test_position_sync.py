"""Mirroring a broker's open positions into our own rows.

sync_snapshots pulled funds and holdings and never positions, so the
Positions page showed nothing for a live account however many the broker
reported. The two are different things -- holdings are shares settled in
demat, positions are open intraday and F&O exposure -- and it is positions a
strategy sizes against.

The broker is the source of truth here, not our fill history: a position
opened outside this platform, or one whose fill we missed, still exists and
must be visible.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

from app.domain.enums import Exchange, ProductType
from app.domain.models import BrokerPosition
from app.services import brokers as broker_service


def account():
    return SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), broker="icici_breeze",
        environment="live",
    )


def reported(symbol="RELIND", quantity=10, average="2800", product=ProductType.CNC):
    return BrokerPosition(
        symbol=symbol, exchange=Exchange.NSE, product=product,
        quantity=quantity, average_price=Decimal(average),
    )


class FakeDb:
    def __init__(self, existing=()):
        self._existing = list(existing)
        self.added = []

    async def execute(self, *a, **kw):
        rows = self._existing
        outer = self

        class Result:
            def scalar_one_or_none(inner):
                # The per-position lookup: match nothing unless seeded.
                return rows[0] if rows else None

            def scalars(inner):
                return iter(outer._existing)

        return Result()

    def add(self, obj):
        self.added.append(obj)


def adapter_with(positions):
    class FakeAdapter:
        async def get_positions(self):
            return positions

    return FakeAdapter()


async def test_a_broker_position_becomes_one_of_ours():
    """The gap that made the page look empty."""
    db = FakeDb()
    count = await broker_service._sync_positions(db, account(), adapter_with([reported()]))
    assert count == 1
    assert db.added, "no Position row was created"
    row = db.added[0]
    assert row.symbol == "RELIND"
    assert row.quantity == 10
    assert row.average_price == Decimal("2800")
    assert row.environment == "live"


async def test_an_existing_row_is_updated_rather_than_duplicated():
    """The table is unique on (account, symbol, exchange, product); inserting
    a second row would violate that and fail the whole sync."""
    existing = SimpleNamespace(
        symbol="RELIND", exchange="NSE", product="CNC", quantity=5,
        average_price=Decimal("2700"), realized_pnl=Decimal("0"), last_price=None,
    )
    db = FakeDb(existing=[existing])
    await broker_service._sync_positions(db, account(), adapter_with([reported(quantity=10)]))
    assert db.added == [], "an existing position was duplicated"
    assert existing.quantity == 10
    assert existing.average_price == Decimal("2800")


async def test_a_closed_position_is_zeroed_not_deleted():
    """realized_pnl on a closed row is part of the day's accounting. Deleting
    it would quietly change the numbers the daily-loss limit reads."""
    closed = SimpleNamespace(
        symbol="INFTEC", exchange="NSE", product="CNC", quantity=8,
        average_price=Decimal("1500"), realized_pnl=Decimal("250"), last_price=None,
    )
    db = FakeDb(existing=[closed])
    # The broker reports a DIFFERENT symbol, so INFTEC is no longer open.
    await broker_service._sync_positions(
        db, account(), adapter_with([reported(symbol="RELIND")])
    )
    assert closed.quantity == 0
    assert closed.realized_pnl == Decimal("250"), "realized P&L was destroyed"


async def test_a_broker_with_no_positions_reports_zero():
    db = FakeDb()
    assert await broker_service._sync_positions(db, account(), adapter_with([])) == 0
