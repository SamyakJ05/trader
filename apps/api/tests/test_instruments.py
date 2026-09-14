"""Instrument master sync.

Kite speaks in numeric instrument tokens; the platform speaks in trading
symbols, and nothing but the instrument master maps between them. A wrong
mapping is worse than a missing one: subscribing to the wrong token delivers
another instrument's prices under a name the user trusts.

The upsert is Postgres-specific, so those tests need a real database.
"""

import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import BrokerAccount, MarketInstrument, User
from app.domain.enums import Exchange
from app.domain.models import Instrument
from app.services import instruments as inst

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="requires disposable Postgres"
)


@pytest.fixture
async def db():
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.execute(
            delete(MarketInstrument).where(MarketInstrument.broker == "zerodha")
        )
        await session.commit()
    await engine.dispose()


@pytest.fixture
async def account(db):
    user = User(
        email=f"inst-{uuid.uuid4().hex[:8]}@x.com",
        password_hash="x",
        is_active=True,
    )
    db.add(user)
    await db.flush()
    row = BrokerAccount(
        user_id=user.id,
        broker="zerodha",
        label=f"Main-{uuid.uuid4().hex[:6]}",
        environment="paper",
        status="connected",
        live_enabled=False,
        credential_ref="ZERODHA_MAIN",
    )
    db.add(row)
    await db.commit()
    return row


def instrument(symbol="RELIANCE", token="738561", **kw):
    defaults = dict(
        symbol=symbol,
        exchange=Exchange.NSE,
        broker_token=token,
        name=f"{symbol} Ltd",
        lot_size=1,
        tick_size=Decimal("0.05"),
        instrument_type="EQ",
    )
    defaults.update(kw)
    return Instrument(**defaults)


class FakeAdapter:
    def __init__(self, rows):
        self._rows = rows

    async def get_instruments(self, exchange=None):
        return self._rows


def patch_adapter(monkeypatch, rows):
    monkeypatch.setattr(inst, "get_adapter", lambda account: FakeAdapter(rows))


# ── syncing ──────────────────────────────────────────────────────────


async def test_instruments_are_written(db, account, monkeypatch):
    patch_adapter(monkeypatch, [instrument("RELIANCE", "738561"), instrument("INFY", "408065")])
    assert await inst.sync_instruments(db, account) == 2

    rows = (await db.execute(select(MarketInstrument))).scalars().all()
    assert {r.symbol for r in rows} == {"RELIANCE", "INFY"}


async def test_a_resync_updates_rather_than_duplicates(db, account, monkeypatch):
    """Tokens move between days for derivatives, so the sync runs daily and
    must not accumulate a second row per symbol each time."""
    patch_adapter(monkeypatch, [instrument("RELIANCE", "738561")])
    await inst.sync_instruments(db, account)

    patch_adapter(monkeypatch, [instrument("RELIANCE", "999999")])
    await inst.sync_instruments(db, account)

    rows = (await db.execute(select(MarketInstrument))).scalars().all()
    assert len(rows) == 1, "a resync must upsert, not insert"
    assert rows[0].broker_token == "999999", "the new token should win"


async def test_a_duplicate_symbol_in_one_dump_does_not_fail_the_batch(
    db, account, monkeypatch
):
    """The dump repeats symbols across segments. A batch carrying the same
    (broker, exchange, symbol) twice would violate the unique constraint and
    lose every row in that batch, not just the duplicate."""
    patch_adapter(
        monkeypatch,
        [instrument("RELIANCE", "738561"), instrument("RELIANCE", "738562")],
    )
    assert await inst.sync_instruments(db, account) == 1


async def test_an_empty_dump_is_refused(db, account, monkeypatch):
    """Silently writing nothing would leave a stale master looking fresh."""
    from app.adapters.base import BrokerError

    patch_adapter(monkeypatch, [])
    with pytest.raises(BrokerError):
        await inst.sync_instruments(db, account)


async def test_blank_symbols_are_skipped(db, account, monkeypatch):
    patch_adapter(monkeypatch, [instrument("RELIANCE"), instrument("   ", "1")])
    assert await inst.sync_instruments(db, account) == 1


async def test_a_large_dump_is_written_in_batches(db, account, monkeypatch):
    """The NSE equity master is tens of thousands of rows; one statement per
    row would take minutes."""
    rows = [instrument(f"SYM{i:05d}", str(100000 + i)) for i in range(2500)]
    patch_adapter(monkeypatch, rows)
    assert await inst.sync_instruments(db, account) == 2500

    count = len((await db.execute(select(MarketInstrument))).scalars().all())
    assert count == 2500


# ── the token map the tick feed needs ────────────────────────────────


async def test_token_map_returns_numeric_tokens(db, account, monkeypatch):
    patch_adapter(monkeypatch, [instrument("RELIANCE", "738561"), instrument("INFY", "408065")])
    await inst.sync_instruments(db, account)

    mapping = await inst.token_map(
        db, broker="zerodha", symbols=["RELIANCE", "INFY"]
    )
    assert mapping == {738561: "RELIANCE", 408065: "INFY"}
    assert all(isinstance(token, int) for token in mapping)


async def test_a_symbol_with_no_token_is_omitted(db, account, monkeypatch):
    """Inventing a token would subscribe to some other instrument and report
    its prices under this symbol."""
    patch_adapter(monkeypatch, [instrument("RELIANCE", "738561"), instrument("WEIRD", None)])
    await inst.sync_instruments(db, account)

    mapping = await inst.token_map(db, broker="zerodha", symbols=["RELIANCE", "WEIRD"])
    assert mapping == {738561: "RELIANCE"}


async def test_an_unknown_symbol_is_simply_absent(db, account, monkeypatch):
    patch_adapter(monkeypatch, [instrument("RELIANCE", "738561")])
    await inst.sync_instruments(db, account)

    mapping = await inst.token_map(db, broker="zerodha", symbols=["NOSUCHTHING"])
    assert mapping == {}


async def test_token_map_with_no_symbols_asks_nothing(db):
    assert await inst.token_map(db, broker="zerodha", symbols=[]) == {}


async def test_token_map_is_scoped_to_its_broker(db, account, monkeypatch):
    """Two brokers can use the same symbol with different tokens; mixing them
    would subscribe to the wrong instrument."""
    patch_adapter(monkeypatch, [instrument("RELIANCE", "738561")])
    await inst.sync_instruments(db, account)

    assert await inst.token_map(db, broker="groww", symbols=["RELIANCE"]) == {}


# ── symbol validation ────────────────────────────────────────────────


async def test_a_known_symbol_resolves(db, account, monkeypatch):
    patch_adapter(monkeypatch, [instrument("RELIANCE", "738561")])
    await inst.sync_instruments(db, account)

    found = await inst.resolve_symbol(db, broker="zerodha", symbol="RELIANCE")
    assert found is not None and found.lot_size == 1


async def test_an_unknown_symbol_resolves_to_nothing(db):
    assert await inst.resolve_symbol(db, broker="zerodha", symbol="NOSUCHTHING") is None
