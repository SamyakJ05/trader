"""Importing market history, as a service both the CLI and the worker call.

A backtest replays stored candles and cannot invent bars it does not have, so
until history exists for a symbol the strategy that trades it cannot be
tested at all. That made importing a prerequisite for the one feature meant
to tell you whether a strategy is worth running -- and it was reachable only
by shell.

The download is a slow bulk fetch from Yahoo, which is why this runs as a
worker job rather than inside a request: a year of daily bars for several
symbols would hold an HTTP connection open for minutes and time out behind
the proxy.

The parsing, validation and split detection live in app.cli.import_history
and are reused verbatim. They refuse a malformed download rather than
dropping rows silently, which matters more here than in the CLI: nobody is
watching a worker's output.
"""

from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.cli.import_history import (
    SOURCE,
    detect_suspect_gaps,
    import_rows,
    normalize_frame,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

# Yahoo appends the venue; the importer speaks NSE tickers.
_YAHOO_SUFFIX = ".NS"


async def import_symbol(
    db: AsyncSession,
    *,
    symbol: str,
    interval: str,
    start: date,
    end: date,
) -> dict:
    """Download and store one symbol's candles. Returns a report.

    The report carries the split suspects rather than only logging them:
    Yahoo's unadjusted data records a split as a genuine overnight collapse,
    and a backtest spanning that date reads it as a price move. Surfacing it
    to the caller is what lets the UI say so at the moment the data lands,
    rather than leaving an inexplicable equity curve to be discovered later.
    """
    import asyncio

    import yfinance as yf

    frame = await asyncio.to_thread(
        yf.download,
        symbol + _YAHOO_SUFFIX,
        start=start,
        end=end,
        interval=interval,
        auto_adjust=False,
        back_adjust=False,
        actions=False,
        progress=False,
        threads=False,
        multi_level_index=False,
        ignore_tz=False,
    )
    rows = normalize_frame(frame, interval)
    await import_rows(db, symbol, interval, rows)
    await db.commit()

    suspects = [
        {
            "date": gap["ts"].date().isoformat(),
            "previous_close": str(gap["previous_close"]),
            "open": str(gap["open"]),
            "move_pct": f"{(gap['ratio'] - 1) * 100:.1f}",
            "likely_split": gap["likely_split"],
        }
        for gap in detect_suspect_gaps(rows)
    ]
    logger.info(
        "history_imported",
        symbol=symbol,
        interval=interval,
        candles=len(rows),
        suspects=len(suspects),
    )
    return {
        "symbol": symbol,
        "interval": interval,
        "source": SOURCE,
        "candles": len(rows),
        "first": rows[0]["ts"].isoformat(),
        "last": rows[-1]["ts"].isoformat(),
        "split_suspects": suspects,
    }
