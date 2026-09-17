"""Daily NSE bhavcopy import: the market universe, for screening and backtests.

The problem this solves. The instrument master names ~5,900 NSE symbols but
carries no prices, so "which stocks can this account afford, and which are
worth looking at" could only be answered by pricing symbols one at a time
through the broker's API -- 40 calls to look at 40 stocks, against a budget of
5,000 a day, and no way to rank what came back. A daily close for every symbol
makes that one query.

WHY BHAVCOPY RATHER THAN A LIVE FEED. These are the exchange's own end-of-day
archive files: official, free, and complete for every symbol, which no live
feed here is. Being a day old is the right trade for the job -- screening
narrows a universe, it does not price a trade. Every order still prices off
the broker's live quote, and nothing in this module is reachable from
reference_price.

The URLs, the two layouts and the column names are taken from a working
implementation (SamyakJ05/StockMarketDB, sources/nse_source.py), which had
already reconciled them against real downloads. NSE serves a current UDiFF
layout and an older legacy one, with different column names for the same
fields; both are tried, and a date with neither is a holiday rather than an
error.
"""

import csv
import datetime as dt
import io
import zipfile
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Candle, MarketInstrument
from app.domain import calendar

logger = get_logger(__name__)

SOURCE = "nse_bhavcopy"
INTERVAL = "1d"
ARCHIVE = "https://nsearchives.nseindia.com"
HOME = "https://www.nseindia.com"

# NSE's archive 403s without a browser-shaped request; the working
# implementation this is taken from warms a session against the homepage
# first, and these headers are what it sends.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{HOME}/all-reports",
}

_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# Series that are ordinary equity. Anything else -- debt, warrants, rights --
# is not what a screen for tradable stock should return.
EQUITY_SERIES = {"EQ", "BE", "BZ", "SM", "ST"}


class BhavcopyUnavailable(Exception):
    """No file for that date. A holiday, or it is not published yet."""


def _urls(day: dt.date) -> list[str]:
    ymd = day.strftime("%Y%m%d")
    legacy = f"{day.day:02d}{_MONTHS[day.month - 1]}{day.year}"
    return [
        f"{ARCHIVE}/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip",
        f"{ARCHIVE}/content/historical/EQUITIES/{day.year}/"
        f"{_MONTHS[day.month - 1]}/cm{legacy}bhav.csv.zip",
    ]


def _pick(row: dict, *names: str) -> str | None:
    """The first column present under any of its names.

    The two layouts spell the same field differently -- ClsPric in UDiFF,
    CLOSE in the legacy file -- so every read names both.
    """
    for name in names:
        value = row.get(name)
        if value not in (None, "", "-", "NA"):
            return value
    return None


def _decimal(row: dict, *names: str) -> Decimal | None:
    raw = _pick(row, *names)
    if raw is None:
        return None
    try:
        value = Decimal(raw.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None
    return value if value.is_finite() else None


def _int(row: dict, *names: str) -> int | None:
    raw = _pick(row, *names)
    if raw is None:
        return None
    try:
        return int(float(raw.replace(",", "")))
    except (ValueError, AttributeError):
        return None


async def fetch(day: dt.date) -> list[dict]:
    """The day's equity rows, or raise BhavcopyUnavailable.

    Both layouts are tried in turn. A 404 on both means no file for that date,
    which is normal for a holiday or a date whose file is not published yet --
    the caller treats that as nothing to do rather than a failure.
    """
    async with httpx.AsyncClient(headers=HEADERS, timeout=60, follow_redirects=True) as client:
        # NSE serves the archive only to a session that has touched the site.
        try:
            await client.get(HOME)
        except httpx.HTTPError:
            pass  # Warm-up is best effort; the download may still succeed.

        for url in _urls(day):
            try:
                resp = await client.get(url)
            except httpx.HTTPError as exc:
                logger.warning("bhavcopy_fetch_error", url=url, error=str(exc))
                continue
            if resp.status_code == 404:
                continue
            if resp.status_code >= 400:
                logger.warning("bhavcopy_http_error", url=url, status=resp.status_code)
                continue
            try:
                return _parse(resp.content)
            except (zipfile.BadZipFile, StopIteration, UnicodeDecodeError) as exc:
                raise BhavcopyUnavailable(f"unreadable archive at {url}: {exc}") from exc

    raise BhavcopyUnavailable(f"no bhavcopy published for {day.isoformat()}")


def _parse(blob: bytes) -> list[dict]:
    """Equity rows from the zipped CSV, normalised across both layouts."""
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        name = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
        text = archive.read(name).decode("utf-8", errors="replace")

    rows = []
    for raw in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}

        symbol = _pick(row, "TckrSymb", "SYMBOL")
        if not symbol:
            continue
        # A combined file would carry derivatives; they are not equity.
        if _pick(row, "FinInstrmTp") in ("STF", "IDF", "STO", "IDO"):
            continue
        series = _pick(row, "SctySrs", "SERIES")
        if series and series not in EQUITY_SERIES:
            continue

        close = _decimal(row, "ClsPric", "CLOSE")
        if close is None or close <= 0:
            # A row with no close prices nothing and screens nothing.
            continue

        rows.append(
            {
                "symbol": symbol,
                "series": series,
                "isin": _pick(row, "ISIN", "ISIN_CODE"),
                "open": _decimal(row, "OpnPric", "OPEN") or close,
                "high": _decimal(row, "HghPric", "HIGH") or close,
                "low": _decimal(row, "LwPric", "LOW") or close,
                "close": close,
                "volume": _int(row, "TtlTradgVol", "TOTTRDQTY"),
                "turnover": _decimal(row, "TtlTrfVal", "TOTTRDVAL"),
            }
        )
    return rows


async def import_day(db: AsyncSession, day: dt.date | None = None) -> int:
    """Store one day's closes as daily candles. Returns rows written.

    Written as Candle rows with interval='1d' and source='nse_bhavcopy'
    rather than into a new table: the backtester and the indicator library
    already read candles, so the history becomes usable by both without
    either learning about bhavcopy. The unique constraint on
    (symbol, exchange, interval, source, ts) makes a re-run an upsert, so
    importing the same day twice is safe.

    Symbols are NSE tickers here, NOT the broker's codes -- a Breeze strategy
    naming RELIND finds nothing under RELIANCE. The ISIN on each row is what
    bridges the two, the same way instruments.history_symbol already does it.
    """
    day = day or _last_trading_day()
    rows = await fetch(day)
    if not rows:
        return 0

    # Midnight IST for the trading date: a daily bar's timestamp is the day it
    # belongs to, and storing it in UTC keeps it comparable with every other
    # candle in the table.
    ts = dt.datetime.combine(day, dt.time(0, 0), tzinfo=calendar.IST).astimezone(
        dt.timezone.utc
    )

    written = 0
    # Chunked: a single statement with ~2,000 rows of parameters is large
    # enough to be worth splitting, and a partial failure then costs one chunk
    # rather than the day.
    for start in range(0, len(rows), 500):
        chunk = rows[start:start + 500]
        stmt = insert(Candle).values(
            [
                {
                    "symbol": r["symbol"],
                    "exchange": "NSE",
                    "interval": INTERVAL,
                    "source": SOURCE,
                    "ts": ts,
                    "open": r["open"],
                    "high": r["high"],
                    "low": r["low"],
                    "close": r["close"],
                    "volume": r["volume"],
                }
                for r in chunk
            ]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "exchange", "interval", "source", "ts"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
            },
        )
        await db.execute(stmt)
        written += len(chunk)

    await db.commit()
    logger.info("bhavcopy_imported", day=day.isoformat(), rows=written)
    return written


def _last_trading_day(today: dt.date | None = None) -> dt.date:
    """The most recent day whose bhavcopy should exist.

    Yesterday, walked back over weekends and holidays. Today's file is not
    published until after the close, so asking for it before then is a 404
    every time.
    """
    day = (today or dt.datetime.now(calendar.IST).date()) - dt.timedelta(days=1)
    for _ in range(10):
        if calendar.is_trading_day(day):
            return day
        day -= dt.timedelta(days=1)
    return day


async def screen(
    db: AsyncSession,
    *,
    broker: str,
    max_price: Decimal,
    min_shares: int = 5,
    min_turnover: Decimal = Decimal("10000000"),
    limit: int = 25,
) -> list[dict]:
    """Affordable, liquid equities from the latest stored bhavcopy.

    One query instead of one broker call per symbol, and it can rank, which
    pricing symbols individually could not.

    `min_turnover` is the liquidity floor, defaulting to Rs 1 crore of traded
    value on the day. It matters more than price: a cheap share nobody trades
    cannot be exited at the screen price, and for small accounts that is the
    likelier way to lose money than picking the wrong direction.

    Returns the broker's own codes, resolved through ISIN, because a symbol
    this platform cannot place an order for is not a candidate. A row whose
    ISIN has no match for this broker is dropped rather than guessed at.
    """
    latest_ts = (
        await db.execute(
            select(Candle.ts)
            .where(Candle.source == SOURCE, Candle.interval == INTERVAL)
            .order_by(Candle.ts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest_ts is None:
        return []

    max_close = max_price / Decimal(min_shares)
    rows = (
        await db.execute(
            select(Candle.symbol, Candle.close, Candle.volume)
            .where(
                Candle.source == SOURCE,
                Candle.interval == INTERVAL,
                Candle.ts == latest_ts,
                Candle.close <= max_close,
                Candle.close > 0,
            )
            .order_by(Candle.volume.desc().nullslast())
            .limit(limit * 8)
        )
    ).all()

    # Resolve NSE tickers to this broker's codes via ISIN. Done in one query
    # rather than per row: the master is ~5,900 rows and the alternative is a
    # round trip each.
    tickers = [r[0] for r in rows]
    isin_by_ticker = dict(
        (
            await db.execute(
                select(MarketInstrument.symbol, MarketInstrument.isin).where(
                    MarketInstrument.exchange == "NSE",
                    MarketInstrument.symbol.in_(tickers),
                    MarketInstrument.isin.isnot(None),
                )
            )
        ).all()
    )
    broker_by_isin = dict(
        (
            await db.execute(
                select(MarketInstrument.isin, MarketInstrument.symbol).where(
                    MarketInstrument.broker == broker,
                    MarketInstrument.exchange == "NSE",
                    MarketInstrument.isin.in_(
                        [v for v in isin_by_ticker.values() if v]
                    ),
                )
            )
        ).all()
    )

    out: list[dict] = []
    for ticker, close, volume in rows:
        if len(out) >= limit:
            break
        turnover = (close or Decimal(0)) * Decimal(volume or 0)
        if turnover < min_turnover:
            continue
        isin = isin_by_ticker.get(ticker)
        code = broker_by_isin.get(isin) if isin else None
        if not code:
            continue
        out.append(
            {
                "symbol": code,
                "nse_ticker": ticker,
                "close": close,
                "turnover": turnover,
                "affordable_shares": int(max_price / close) if close else 0,
            }
        )
    return out
