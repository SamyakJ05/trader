"""Instrument master sync.

Kite's socket and order API speak in numeric instrument tokens; the rest of the
platform speaks in trading symbols. The instrument master is what maps between
them, and nothing else can: a token is not derivable from a symbol.

Zerodha publishes the master as a CSV dump, refreshed daily. Tokens are not
stable across days for derivatives, so this is a scheduled sync rather than a
one-off import.
"""

import uuid

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import BrokerError
from app.adapters.registry import get_adapter
from app.core.logging import get_logger
from app.db.models import BrokerAccount, MarketInstrument
from app.domain.enums import AuditEventType
from app.services import audit

logger = get_logger(__name__)

# Written in batches: the NSE equity master alone is tens of thousands of rows,
# and one statement per row would take minutes.
BATCH_SIZE = 1000


async def sync_instruments(
    db: AsyncSession, account: BrokerAccount, *, exchange: str = "NSE"
) -> int:
    """Refresh the instrument master for one broker and exchange.

    Upserts on (broker, exchange, symbol), so a re-run is safe and picks up
    tokens that changed overnight. Returns the number of rows written.
    """
    adapter = get_adapter(account)
    instruments = await adapter.get_instruments(exchange)
    if not instruments:
        raise BrokerError(f"Broker returned no instruments for {exchange}")

    written = 0
    batch: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for instrument in instruments:
        symbol = (instrument.symbol or "").strip()
        if not symbol:
            continue
        exchange_value = getattr(instrument.exchange, "value", instrument.exchange)
        # The whole contract, not just the symbol. Deduping on the symbol
        # alone would have collapsed an entire options chain to one row before
        # the database ever saw it -- Breeze lists 3,350 NIFTY contracts under
        # that one code -- so every strike and expiry but the first was
        # discarded here, silently.
        key = (
            exchange_value,
            symbol,
            instrument.expiry,
            instrument.strike,
            instrument.option_right,
        )
        if key in seen:
            # The dump can still repeat an exact contract; a batch carrying
            # the same key twice would fail the whole statement.
            continue
        seen.add(key)
        batch.append(
            {
                "id": uuid.uuid4(),
                "broker": account.broker,
                "broker_token": str(instrument.broker_token)
                if instrument.broker_token
                else None,
                "symbol": symbol,
                "name": instrument.name,
                "exchange": exchange_value,
                # The domain Instrument carries no segment; the column stays
                # null until an adapter has a reason to populate it.
                "segment": None,
                "instrument_type": instrument.instrument_type,
                "lot_size": instrument.lot_size,
                "tick_size": instrument.tick_size,
                "expiry": instrument.expiry,
                "strike": instrument.strike,
                "option_right": instrument.option_right,
                "isin": instrument.isin,
            }
        )
        if len(batch) >= BATCH_SIZE:
            written += await _write_batch(db, batch)
            batch = []

    if batch:
        written += await _write_batch(db, batch)

    await audit.emit(
        db,
        AuditEventType.BROKER_SYNC,
        user_id=account.user_id,
        entity_type="broker_account",
        entity_id=account.id,
        payload={"action": "instruments_sync", "exchange": exchange, "count": written},
    )
    await db.commit()
    logger.info(
        "instruments_synced", broker=account.broker, exchange=exchange, count=written
    )
    return written


async def _write_batch(db: AsyncSession, rows: list[dict]) -> int:
    """Upsert one batch, leaving existing ids alone.

    The conflict target must match migration 0010's functional unique index
    expression for expression: a derivatives contract is identified by its
    expiry, strike and right as well as its symbol, and naming only
    (broker, exchange, symbol) here would raise
    "no unique or exclusion constraint matching the ON CONFLICT
    specification" now that the plain constraint is gone.
    """
    statement = insert(MarketInstrument).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[
            "broker",
            "exchange",
            "symbol",
            text("COALESCE(expiry, DATE '1900-01-01')"),
            text("COALESCE(strike, -1)"),
            text("COALESCE(option_right, '')"),
        ],
        set_={
            "broker_token": statement.excluded.broker_token,
            "name": statement.excluded.name,
            "instrument_type": statement.excluded.instrument_type,
            "lot_size": statement.excluded.lot_size,
            "tick_size": statement.excluded.tick_size,
            "isin": statement.excluded.isin,
        },
    )
    await db.execute(statement)
    return len(rows)


async def token_map(
    db: AsyncSession, *, broker: str, symbols: list[str], exchange: str = "NSE"
) -> dict[int, str]:
    """Numeric instrument token -> trading symbol, for the tick feed.

    Symbols with no token are omitted rather than guessed at: subscribing to a
    token we invented would either error or, worse, deliver another
    instrument's prices under the wrong name.
    """
    if not symbols:
        return {}
    result = await db.execute(
        select(MarketInstrument.broker_token, MarketInstrument.symbol).where(
            MarketInstrument.broker == broker,
            MarketInstrument.exchange == exchange,
            MarketInstrument.symbol.in_(symbols),
            MarketInstrument.broker_token.isnot(None),
        )
    )
    mapping: dict[int, str] = {}
    for token, symbol in result:
        try:
            mapping[int(token)] = symbol
        except (TypeError, ValueError):
            logger.warning("instrument_token_not_numeric", symbol=symbol, token=token)
    missing = set(symbols) - set(mapping.values())
    if missing:
        logger.warning(
            "instruments_missing_tokens",
            broker=broker,
            exchange=exchange,
            symbols=sorted(missing),
        )
    return mapping


async def resolve_symbol(
    db: AsyncSession, *, broker: str, symbol: str, exchange: str = "NSE"
) -> MarketInstrument | None:
    """One instrument, for validating an order's symbol before it is placed."""
    result = await db.execute(
        select(MarketInstrument).where(
            MarketInstrument.broker == broker,
            MarketInstrument.exchange == exchange,
            MarketInstrument.symbol == symbol,
        )
    )
    return result.scalar_one_or_none()


async def unknown_symbols(
    db: AsyncSession, *, broker: str, symbols: list[str], exchange: str = "NSE"
) -> list[str]:
    """Which of these symbols the broker's instrument master does not contain.

    Brokers name the same instrument differently — Breeze calls RELIANCE
    something like RELIND — and this platform stores each broker's own codes.
    A strategy naming a symbol its broker does not recognise finds no candles
    and emits no signals, which is indistinguishable from a quiet market. This
    is what lets that be refused rather than discovered.

    An empty master means we cannot judge, so nothing is reported unknown: a
    strategy should not be blocked because a sync has not run yet.
    """
    if not symbols:
        return []
    known = (
        await db.execute(
            select(MarketInstrument.symbol).where(
                MarketInstrument.broker == broker,
                MarketInstrument.exchange == exchange,
            )
        )
    ).scalars()
    known_set = set(known)
    if not known_set:
        logger.info(
            "instrument_master_empty", broker=broker, exchange=exchange,
            detail="cannot validate symbols; allowing",
        )
        return []
    return [s for s in symbols if s not in known_set]


async def history_symbol(
    db: AsyncSession, *, broker: str, symbol: str, exchange: str = "NSE"
) -> str | None:
    """The symbol that imported market history is stored under, for a symbol
    named in one broker's own codes.

    Brokers use private codes -- Breeze's RELIND is the NSE's RELIANCE -- and
    imported history is keyed by the NSE ticker. Without this a Breeze
    strategy cannot be backtested at all: its symbols match no candle.

    The bridge is the ISIN, which is the same at every venue. Returns None
    when no mapping exists rather than guessing; a backtest against the wrong
    instrument is worse than no backtest.
    """
    row = (
        await db.execute(
            select(MarketInstrument.isin).where(
                MarketInstrument.broker == broker,
                MarketInstrument.exchange == exchange,
                MarketInstrument.symbol == symbol,
                MarketInstrument.isin.isnot(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        # Nothing to translate through. The symbol may already be the one
        # history uses -- which is the case for Kite, whose codes are NSE
        # tickers -- so it is handed back unchanged rather than refused.
        return symbol

    # Any other broker's row carrying the same ISIN names the same security.
    match = (
        await db.execute(
            select(MarketInstrument.symbol).where(
                MarketInstrument.isin == row,
                MarketInstrument.exchange == exchange,
                MarketInstrument.broker != broker,
            )
        )
    ).scalars().first()
    return match or symbol
