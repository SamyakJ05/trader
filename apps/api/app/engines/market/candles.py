"""Persistent minute candles. Missing minutes are gaps, never fabricated prices."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Candle
from app.domain.calendar import IST

INTERVALS = {"1m": timedelta(minutes=1), "5m": timedelta(minutes=5), "1d": timedelta(days=1)}


def bucket_start(ts, interval):
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("Timestamp must include timezone")
    if interval not in INTERVALS:
        raise ValueError("Unsupported candle interval")
    local = ts.astimezone(IST)
    if interval == "1d":
        return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    minutes = int(INTERVALS[interval].total_seconds() // 60)
    return local.replace(
        minute=local.minute // minutes * minutes, second=0, microsecond=0
    ).astimezone(timezone.utc)


def rollup(rows):
    rows = sorted(rows, key=lambda row: row.ts)
    if len(rows) != 5:
        return None
    start = bucket_start(rows[0].ts, "5m")
    if [row.ts for row in rows] != [start + timedelta(minutes=i) for i in range(5)]:
        return None
    return dict(
        ts=start,
        open=rows[0].open,
        high=max(r.high for r in rows),
        low=min(r.low for r in rows),
        close=rows[-1].close,
        volume=sum(r.volume for r in rows) if all(r.volume is not None for r in rows) else None,
    )


async def store_candle(db, *, symbol, exchange, interval, source, **values):
    stmt = insert(Candle).values(
        symbol=symbol, exchange=exchange, interval=interval, source=source, **values
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=["symbol", "exchange", "interval", "source", "ts"],
            set_={key: getattr(stmt.excluded, key) for key in values if key != "ts"},
        )
    )


async def record_tick(db, tick, source="simulator"):
    start = bucket_start(tick.ts, "1m")
    stmt = insert(Candle).values(
        symbol=tick.symbol,
        exchange=tick.exchange.value,
        interval="1m",
        source=source,
        ts=start,
        open=tick.last_price,
        high=tick.last_price,
        low=tick.last_price,
        close=tick.last_price,
        volume=None,
        first_tick_at=tick.ts,
        last_tick_at=tick.ts,
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=["symbol", "exchange", "interval", "source", "ts"],
            set_={
                "high": func.greatest(Candle.high, tick.last_price),
                "low": func.least(Candle.low, tick.last_price),
                "open": case((Candle.first_tick_at > tick.ts, tick.last_price), else_=Candle.open),
                "close": case((Candle.last_tick_at < tick.ts, tick.last_price), else_=Candle.close),
                "first_tick_at": func.least(Candle.first_tick_at, tick.ts),
                "last_tick_at": func.greatest(Candle.last_tick_at, tick.ts),
            },
        )
    )
    # Recompute the previous bucket on each tick, and the changed bucket if
    # this is a late tick. Upserts make worker retries harmless.
    now = datetime.now(timezone.utc)
    for bucket in {bucket_start(start, "5m"), bucket_start(start, "5m") - INTERVALS["5m"]}:
        if bucket + INTERVALS["5m"] > now:
            continue
        rows = (
            (
                await db.execute(
                    select(Candle).where(
                        Candle.symbol == tick.symbol,
                        Candle.exchange == tick.exchange.value,
                        Candle.interval == "1m",
                        Candle.source == source,
                        Candle.ts >= bucket,
                        Candle.ts < bucket + INTERVALS["5m"],
                    )
                )
            )
            .scalars()
            .all()
        )
        values = rollup(rows)
        if values:
            await store_candle(
                db,
                symbol=tick.symbol,
                exchange=tick.exchange.value,
                interval="5m",
                source=source,
                **values,
            )


async def history(db, symbol, exchange, interval, source, limit, now=None):
    cutoff = bucket_start(now or datetime.now(timezone.utc), interval)
    rows = (
        (
            await db.execute(
                select(Candle)
                .where(
                    Candle.symbol == symbol,
                    Candle.exchange == exchange,
                    Candle.interval == interval,
                    Candle.source == source,
                    Candle.ts < cutoff,
                )
                .order_by(Candle.ts.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(reversed(rows))


async def last_close(db, *, symbol, exchange, source="kite", max_age_seconds=None, now=None):
    """The most recent completed candle's close, or None.

    Used to price a live order when no tick is cached. Returns None rather
    than the newest row available when that row is older than
    `max_age_seconds`: a close from before lunch says nothing about the market
    now, and valuing an order against it could pass a limit the real price
    would fail.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = bucket_start(moment, "1m")
    row = (
        await db.execute(
            select(Candle)
            .where(
                Candle.symbol == symbol,
                Candle.exchange == exchange,
                Candle.interval == "1m",
                Candle.source == source,
                Candle.ts < cutoff,
            )
            .order_by(Candle.ts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if max_age_seconds is not None:
        ts = row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=timezone.utc)
        # A one-minute candle is stamped at its start, so its close is up to a
        # minute newer than its timestamp; allow for that before judging age.
        age = (moment - ts).total_seconds() - 60
        if age > max_age_seconds:
            return None
    return row.close
