"""Import Yahoo NSE candles: python -m app.cli.import_history --help."""

import argparse
import asyncio
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from itertools import pairwise

from app.domain.calendar import IST
from app.engines.market.candles import bucket_start, store_candle

SOURCE = "yfinance_unadjusted"


def normalize_frame(frame, interval, now=None):
    """Validate the complete download before any writes; no silent NaN drops."""
    if frame is None or frame.empty:
        raise ValueError("Yahoo returned no candles; check symbol, dates and intraday retention")
    cutoff = bucket_start(now or datetime.now(timezone.utc), interval)
    rows = []
    seen = set()
    for stamp, record in frame.sort_index().iterrows():
        ts = stamp.to_pydatetime()
        if ts.tzinfo is None:
            if interval != "1d":
                raise ValueError("Intraday timestamps must carry a timezone")
            ts = ts.replace(tzinfo=IST)
        ts = ts.astimezone(timezone.utc)
        if ts != bucket_start(ts, interval):
            raise ValueError("Candle timestamp is not aligned to its interval")
        if ts >= cutoff:
            continue  # An in-progress bar must never enter a historical replay.
        if ts in seen:
            raise ValueError("Duplicate candle timestamps in download")
        seen.add(ts)
        row = dict(ts=ts)
        for key in ("open", "high", "low", "close"):
            value = Decimal(str(record[key.title()]))
            if not value.is_finite() or value <= 0:
                raise ValueError(f"Invalid {key} at {ts}")
            row[key] = value.quantize(Decimal("0.0001"))
        volume = Decimal(str(record["Volume"]))
        if not volume.is_finite() or volume < 0 or volume != volume.to_integral_value():
            raise ValueError(f"Invalid volume at {ts}")
        row["volume"] = int(volume)
        if row["high"] < max(row["open"], row["close"]) or row["low"] > min(
            row["open"], row["close"]
        ):
            raise ValueError(f"Invalid OHLC bounds at {ts}")
        rows.append(row)
    if not rows:
        raise ValueError("No completed candles in download")
    return rows


# A 2:1 split halves the price overnight; a 1:5 drops it 80%. Unadjusted data
# records that as a real move, so every downstream consumer -- strategy signals,
# drawdown, VaR -- treats a corporate action as a crash. Genuine 40% overnight
# moves do happen, so this warns rather than rejects.
SPLIT_SUSPECT_MOVE = Decimal("0.40")

# Ratios a real split is likely to land near, within a few percent.
_COMMON_SPLIT_RATIOS = (
    (Decimal("0.5"), "2:1"),
    (Decimal("0.2"), "5:1"),
    (Decimal("0.1"), "10:1"),
    (Decimal("0.25"), "4:1"),
    (Decimal(2), "1:2 reverse"),
    (Decimal(5), "1:5 reverse"),
)


def detect_suspect_gaps(rows: list[dict]) -> list[dict]:
    """Bar-to-bar moves large enough to suggest an unadjusted corporate action.

    Returns the suspects rather than raising: the importer reports them so the
    operator can decide, because this cannot distinguish a split from a crash.
    """
    suspects = []
    for previous, current in pairwise(rows):
        if previous["close"] <= 0:
            continue
        ratio = current["open"] / previous["close"]
        move = abs(ratio - Decimal(1))
        if move < SPLIT_SUSPECT_MOVE:
            continue
        match = next(
            (
                name
                for value, name in _COMMON_SPLIT_RATIOS
                if abs(ratio - value) / value < Decimal("0.05")
            ),
            None,
        )
        suspects.append(
            {
                "ts": current["ts"],
                "previous_close": previous["close"],
                "open": current["open"],
                "ratio": ratio,
                "likely_split": match,
            }
        )
    return suspects


async def import_rows(db, symbol, interval, rows):
    for row in rows:
        await store_candle(
            db, symbol=symbol, exchange="NSE", interval=interval, source=SOURCE, **row
        )


async def main(args):
    import yfinance as yf

    from app.db.session import async_session_factory, engine

    frame = await asyncio.to_thread(
        yf.download,
        args.symbol + ".NS",
        start=args.start,
        end=args.end,
        interval=args.interval,
        auto_adjust=False,
        back_adjust=False,
        actions=False,
        progress=False,
        threads=False,
        multi_level_index=False,
        ignore_tz=False,
    )
    rows = normalize_frame(frame, args.interval)
    async with async_session_factory() as db:
        await import_rows(db, args.symbol, args.interval, rows)
        await db.commit()
    await engine.dispose()
    print(f"Imported {len(rows)} {args.interval} candles for {args.symbol} ({SOURCE})")

    # Unadjusted data records a split as a real overnight move, which every
    # downstream metric then treats as a crash. Say so at import time rather
    # than letting it surface as an inexplicable equity curve weeks later.
    for gap in detect_suspect_gaps(rows):
        note = f" — looks like a {gap['likely_split']} split" if gap["likely_split"] else ""
        print(
            f"  WARNING {gap['ts'].date()}: {gap['previous_close']} -> "
            f"{gap['open']} ({(gap['ratio'] - 1) * 100:.1f}%){note}. "
            "Unadjusted data does not correct corporate actions; a backtest "
            "spanning this date will read it as a price move."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True, help="NSE symbol, e.g. RELIANCE (without .NS)")
    parser.add_argument("--interval", choices=["1m", "5m", "1d"], default="1d")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat, help="Exclusive end date")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Z0-9&_-]{1,64}", args.symbol) or args.start >= args.end:
        parser.error("Use a valid uppercase NSE symbol and start before end")
    try:
        asyncio.run(main(args))
    except (ValueError, ImportError) as exc:
        parser.exit(1, f"Import failed: {exc}\n")
