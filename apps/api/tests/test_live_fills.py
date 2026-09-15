"""Live fill accounting.

The paper engine books its own fills. Live orders have none of that: the
broker fills them and we learn about it from a postback. Without this module a
live fill updates the order row and nothing else — positions would not move,
and the realized-P&L counter MAX_DAILY_LOSS reads would stay at zero however
much the account lost.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import fakeredis.aioredis

from app.services import daily_pnl, live_fills


def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def account(environment="live"):
    return SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), broker="zerodha",
        environment=environment, credential_ref="ZERODHA_MAIN",
    )


def order(environment="live", side="BUY", product="CNC"):
    return SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), broker_account_id=uuid.uuid4(),
        environment=environment, side=side, product=product, symbol="RELIANCE",
        exchange="NSE", client_order_id="cli-1",
        placed_at=datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc),
    )


class FakeDb:
    """`booked` is the quantity already recorded against this order.

    Serves two shapes of query, because booking a fill now runs under the
    account lock: lock_account() does a SELECT ... FOR UPDATE and calls
    scalar_one(), while the dedupe sums fill quantities via scalars(). A fake
    that only answered one of them would make the lock look optional.
    """

    def __init__(self, booked=0, balance=Decimal("100000")):
        self._booked = booked
        self._balance = balance
        self.added = []
        self.locked = 0

    async def execute(self, *args, **kwargs):
        booked = self._booked
        self_ = self

        class Result:
            def scalars(self):
                return iter([booked] if booked else [])

            def scalar_one(self):
                # lock_account's SELECT ... FOR UPDATE on the account row.
                self_.locked += 1
                return SimpleNamespace(id=uuid.uuid4())

            def scalar_one_or_none(self):
                # ledger.latest() -- no prior rows in these fixtures.
                return None

        return Result()

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        pass


def patch_position(monkeypatch, realized=Decimal("0")):
    async def fake_apply(db, order_row, qty, price):
        return SimpleNamespace(), realized

    monkeypatch.setattr(live_fills, "_apply_fill_to_position", fake_apply)


# ── environment ──────────────────────────────────────────────────────


async def test_paper_fills_are_not_booked_here(monkeypatch):
    """The simulator books its own; routing them through here too would
    double-count every position."""
    patch_position(monkeypatch)
    result = await live_fills.book_fill(
        FakeDb(), redis(), account=account("paper"), order=order("paper"),
        filled_quantity=10, average_price=Decimal("100"),
    )
    assert result is None


# ── the daily-loss counter ───────────────────────────────────────────


async def test_a_live_fill_feeds_the_daily_loss_counter(monkeypatch):
    """The control this module exists for. Before it, a live account could
    lose any amount and MAX_DAILY_LOSS would never fire."""
    patch_position(monkeypatch, realized=Decimal("-5000"))
    r = redis()
    o = order()
    await live_fills.book_fill(
        FakeDb(), r, account=account(), order=o,
        filled_quantity=10, average_price=Decimal("100"), charges=Decimal("20"),
    )
    realized = await daily_pnl.get_realized(r, o.user_id, "live")
    assert realized == Decimal("-5020"), "loss and charges should both count"


async def test_charges_reduce_a_winning_fill(monkeypatch):
    patch_position(monkeypatch, realized=Decimal("1000"))
    r = redis()
    o = order()
    await live_fills.book_fill(
        FakeDb(), r, account=account(), order=o,
        filled_quantity=10, average_price=Decimal("100"), charges=Decimal("150"),
    )
    assert await daily_pnl.get_realized(r, o.user_id, "live") == Decimal("850")


# ── cumulative quantities ────────────────────────────────────────────


async def test_only_the_unbooked_part_of_a_fill_is_recorded(monkeypatch):
    """Brokers report cumulative quantity. Booking the reported figure blindly
    would double the position every time Kite resends a postback."""
    patch_position(monkeypatch)
    db = FakeDb(booked=6)
    fill = await live_fills.book_fill(
        db, redis(), account=account(), order=order(),
        filled_quantity=10, average_price=Decimal("100"),
    )
    assert fill.quantity == 4


async def test_a_repeated_postback_books_nothing(monkeypatch):
    patch_position(monkeypatch)
    assert await live_fills.book_fill(
        FakeDb(booked=10), redis(), account=account(), order=order(),
        filled_quantity=10, average_price=Decimal("100"),
    ) is None


async def test_a_zero_fill_books_nothing(monkeypatch):
    patch_position(monkeypatch)
    assert await live_fills.book_fill(
        FakeDb(), redis(), account=account(), order=order(),
        filled_quantity=0, average_price=Decimal("100"),
    ) is None


# ── charges ──────────────────────────────────────────────────────────


async def test_broker_charges_are_used_when_supplied(monkeypatch):
    patch_position(monkeypatch)
    fill = await live_fills.book_fill(
        FakeDb(), redis(), account=account(), order=order(),
        filled_quantity=10, average_price=Decimal("100"), charges=Decimal("42.50"),
    )
    assert fill.charges == Decimal("42.50")


async def test_charges_are_estimated_when_the_broker_does_not_say(monkeypatch):
    """Kite does not itemise charges on a postback. An estimate is better than
    zero, and the audit records which it was."""
    patch_position(monkeypatch)
    fill = await live_fills.book_fill(
        FakeDb(), redis(), account=account(), order=order(),
        filled_quantity=10, average_price=Decimal("1000"),
    )
    assert fill.charges > 0


# ── payload parsing ──────────────────────────────────────────────────


async def test_a_payload_without_an_average_price_books_nothing(monkeypatch):
    patch_position(monkeypatch)
    assert await live_fills.book_from_payload(
        FakeDb(), redis(), account=account(), order=order(),
        payload={"filled_quantity": 10},
    ) is None


async def test_a_malformed_payload_books_nothing(monkeypatch):
    patch_position(monkeypatch)
    for payload in (
        {"filled_quantity": "many", "average_price": 100},
        {"filled_quantity": 10, "average_price": "cheap"},
        {"filled_quantity": 10, "average_price": -5},
    ):
        assert await live_fills.book_from_payload(
            FakeDb(), redis(), account=account(), order=order(), payload=payload
        ) is None


async def test_a_well_formed_payload_books_the_fill(monkeypatch):
    patch_position(monkeypatch)
    fill = await live_fills.book_from_payload(
        FakeDb(), redis(), account=account(), order=order(),
        payload={"filled_quantity": 5, "average_price": 2845.5},
    )
    assert fill is not None and fill.quantity == 5


# ── the account lock and the cash ledger ─────────────────────────────
# Both were missing entirely: book_fill updated the position and the
# daily-P&L counter but never took the lock and never moved cash. The
# paper engine does both for its own fills. A live account's ledger
# therefore drifted from its fills permanently, and the dedupe below --
# a read-then-write -- could not defend itself against the concurrent
# postbacks brokers are documented to send.


async def test_the_lock_is_taken_before_the_dedupe_read(monkeypatch):
    """Ordering is the whole point, so assert ordering rather than presence.

    ledger.append takes the lock itself, so 'was the lock ever taken' passes
    even with the explicit lock removed -- it just happens later, after the
    read-then-write it was supposed to protect. What matters is that the lock
    precedes the already-booked SELECT: two concurrent postbacks that both
    read before either locks will both book the same fill.
    """
    patch_position(monkeypatch)
    db = FakeDb()
    calls: list[str] = []

    real_execute = db.execute

    async def tracking_execute(*args, **kwargs):
        result = await real_execute(*args, **kwargs)

        class Tracked:
            def scalars(self_inner):
                calls.append("read_booked")
                return result.scalars()

            def scalar_one(self_inner):
                calls.append("lock")
                return result.scalar_one()

            def scalar_one_or_none(self_inner):
                return result.scalar_one_or_none()

        return Tracked()

    db.execute = tracking_execute
    await live_fills.book_fill(
        db, redis(), account=account(), order=order(),
        filled_quantity=5, average_price=Decimal("100"),
    )
    assert "lock" in calls and "read_booked" in calls
    assert calls.index("lock") < calls.index("read_booked"), (
        f"lock must precede the dedupe read, got {calls}"
    )


async def test_a_live_buy_moves_cash_out_of_the_ledger(monkeypatch):
    patch_position(monkeypatch)
    db = FakeDb()
    await live_fills.book_fill(
        db, redis(), account=account(), order=order(side="BUY"),
        filled_quantity=2, average_price=Decimal("100"),
        charges=Decimal("5"),
    )
    # OPENING rows appear too: ledger.append seeds one when an account has no
    # history, and this fake always reports none. Assert on the entries this
    # test is about rather than the total count.
    by_type = {
        e.entry_type: e.amount
        for e in db.added
        if type(e).__name__ == "CashLedger"
    }
    assert by_type["BUY"] == Decimal("-200"), "a buy must debit notional"
    assert by_type["CHARGES"] == Decimal("-5"), "charges always debit"


async def test_a_live_sell_credits_the_ledger(monkeypatch):
    patch_position(monkeypatch)
    db = FakeDb()
    await live_fills.book_fill(
        db, redis(), account=account(), order=order(side="SELL"),
        filled_quantity=2, average_price=Decimal("100"),
        charges=Decimal("5"),
    )
    by_type = {
        e.entry_type: e.amount
        for e in db.added
        if type(e).__name__ == "CashLedger"
    }
    assert by_type["SELL"] == Decimal("200"), "a sell must credit notional"
    assert by_type["CHARGES"] == Decimal("-5")


async def test_only_the_unbooked_delta_moves_cash(monkeypatch):
    """Postbacks report cumulative quantities. The ledger must move for the
    new part only, or a resent postback double-charges the account."""
    patch_position(monkeypatch)
    db = FakeDb(booked=3)
    await live_fills.book_fill(
        db, redis(), account=account(), order=order(side="BUY"),
        filled_quantity=5, average_price=Decimal("100"),
        charges=Decimal("1"),
    )
    by_type = {
        e.entry_type: e.amount
        for e in db.added
        if type(e).__name__ == "CashLedger"
    }
    assert by_type["BUY"] == Decimal("-200"), "only the 2 unbooked shares"
