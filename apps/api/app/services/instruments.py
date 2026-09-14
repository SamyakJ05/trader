"""Instrument master sync.

Kite's socket and order API speak in numeric instrument tokens; the rest of the
platform speaks in trading symbols. The instrument master is what maps between
them, and nothing else can: a token is not derivable from a symbol.

Zerodha publishes the master as a CSV dump, refreshed daily. Tokens are not
stable across days for derivatives, so this is a scheduled sync rather than a
one-off import.
"""

import uuid

from sqlalchemy import select
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
        key = (exchange_value, symbol)
        if key in seen:
            # The dump can repeat a symbol across segments; the unique
            # constraint is on (broker, exchange, symbol), so a batch carrying
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
    """Upsert one batch, leaving existing ids alone."""
    statement = insert(MarketInstrument).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=["broker", "exchange", "symbol"],
        set_={
            "broker_token": statement.excluded.broker_token,
            "name": statement.excluded.name,
            "instrument_type": statement.excluded.instrument_type,
            "lot_size": statement.excluded.lot_size,
            "tick_size": statement.excluded.tick_size,
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
