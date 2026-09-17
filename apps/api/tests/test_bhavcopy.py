"""The daily bhavcopy import, and the screen it exists to make possible.

The column names and both URL layouts come from a working implementation that
had already reconciled them against real downloads. What is worth asserting
here is that the parser handles BOTH layouts -- NSE serves a current UDiFF
file and an older legacy one, spelling the same fields differently -- and that
the screen returns only what this platform could actually place an order for.
"""

import datetime as dt
import io
import zipfile
from decimal import Decimal

from app.services import bhavcopy

# A real UDiFF header, in the order NSE serves it.
UDIFF = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,"
    "XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,"
    "LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
    "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,"
    "Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n"
    "2026-09-16,2026-09-16,CM,NSE,STK,1234,INE002A01018,RELIANCE,EQ,,,,,"
    "RELIANCE INDUSTRIES,1240.00,1250.00,1235.00,1244.00,1244.00,1238.00,,,,,"
    "5000000,6220000000,120000,F1,1,,,,,\n"
    "2026-09-16,2026-09-16,CM,NSE,STK,1235,INE009A01021,INFY,EQ,,,,,"
    "INFOSYS,1500.00,1510.00,1495.00,1505.00,1505.00,1498.00,,,,,"
    "3000000,4515000000,90000,F1,1,,,,,\n"
)

# The legacy layout, which spells every field differently.
LEGACY = (
    "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,"
    "TIMESTAMP,TOTALTRADES,ISIN\n"
    "RELIANCE,EQ,1240.00,1250.00,1235.00,1244.00,1244.00,1238.00,5000000,"
    "6220000000,16-SEP-2026,120000,INE002A01018\n"
)


def zipped(csv_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("bhav.csv", csv_text)
    return buf.getvalue()


# ── parsing ──────────────────────────────────────────────────────────


def test_the_current_udiff_layout_parses():
    rows = bhavcopy._parse(zipped(UDIFF))
    assert len(rows) == 2
    reliance = next(r for r in rows if r["symbol"] == "RELIANCE")
    assert reliance["close"] == Decimal("1244.00")
    assert reliance["isin"] == "INE002A01018"
    assert reliance["volume"] == 5000000


def test_the_legacy_layout_parses_too():
    """Same fields, different column names. A parser that handled only one
    would silently return nothing for half the date range."""
    rows = bhavcopy._parse(zipped(LEGACY))
    assert len(rows) == 1
    assert rows[0]["close"] == Decimal("1244.00")
    assert rows[0]["isin"] == "INE002A01018"


def test_non_equity_series_are_dropped():
    """A screen for tradable stock should not return debt or rights."""
    csv_text = UDIFF + (
        "2026-09-16,2026-09-16,CM,NSE,STK,9,INE999A01011,SOMEBOND,N1,,,,,"
        "A BOND,100,100,100,100,100,100,,,,,10,1000,5,F1,1,,,,,\n"
    )
    symbols = {r["symbol"] for r in bhavcopy._parse(zipped(csv_text))}
    assert "SOMEBOND" not in symbols


def test_derivative_rows_are_dropped_if_ever_present():
    csv_text = UDIFF + (
        "2026-09-16,2026-09-16,FO,NSE,STO,9,,NIFTY,,2026-09-29,,25000,CE,"
        "NIFTY CE,10,12,9,11,11,10,,,,,100,1100,50,F1,50,,,,,\n"
    )
    symbols = {r["symbol"] for r in bhavcopy._parse(zipped(csv_text))}
    assert "NIFTY" not in symbols


def test_a_row_with_no_close_is_skipped_not_zeroed():
    """A zero close would screen as infinitely affordable."""
    csv_text = (
        "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,ISIN\n"
        "BROKEN,EQ,10,10,10,,INE000A01000\n"
    )
    assert bhavcopy._parse(zipped(csv_text)) == []


def test_missing_ohlc_falls_back_to_the_close():
    """Some rows carry only a close. Better a flat bar than no bar: the close
    is the number the screen uses."""
    csv_text = (
        "SYMBOL,SERIES,CLOSE,ISIN\nTHINCO,EQ,250.00,INE000A01000\n"
    )
    row = bhavcopy._parse(zipped(csv_text))[0]
    assert row["open"] == row["high"] == row["low"] == Decimal("250.00")


# ── which day to ask for ─────────────────────────────────────────────


def test_it_asks_for_a_trading_day_not_today():
    """Today's file is not published until after the close, so asking for it
    during the session is a 404 every time."""
    # A Monday: the previous trading day is the Friday before.
    monday = dt.date(2026, 9, 21)
    assert bhavcopy._last_trading_day(monday) == dt.date(2026, 9, 18)


def test_both_url_layouts_are_tried():
    urls = bhavcopy._urls(dt.date(2026, 9, 16))
    assert any("BhavCopy_NSE_CM" in u for u in urls), "current UDiFF layout"
    assert any("cm16SEP2026bhav" in u for u in urls), "legacy layout"


# ── the screen ───────────────────────────────────────────────────────


class Row(tuple):
    pass


class FakeResult:
    def __init__(self, rows, scalar=None):
        self._rows = rows
        self._scalar = scalar

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._scalar


class ScreenDb:
    """Answers the screen's four queries in order."""

    def __init__(self, latest_ts, candles, isin_rows, broker_rows):
        self._results = [
            FakeResult([], scalar=latest_ts),
            FakeResult(candles),
            FakeResult(isin_rows),
            FakeResult(broker_rows),
        ]

    async def execute(self, *a, **kw):
        return self._results.pop(0)


async def test_the_screen_returns_broker_codes_not_nse_tickers():
    """A candidate the platform cannot place an order for is not a candidate.
    Breeze needs RELIND; RELIANCE would be refused at creation."""
    db = ScreenDb(
        latest_ts=dt.datetime(2026, 9, 16, tzinfo=dt.timezone.utc),
        candles=[("TATAMOTORS", Decimal("400"), 5_000_000)],
        isin_rows=[("TATAMOTORS", "INE155A01022")],
        broker_rows=[("INE155A01022", "TATMOT")],
    )
    found = await bhavcopy.screen(
        db, broker="icici_breeze", max_price=Decimal("2500")
    )
    assert [r["symbol"] for r in found] == ["TATMOT"]
    assert found[0]["nse_ticker"] == "TATAMOTORS"
    assert found[0]["affordable_shares"] == 6


async def test_a_symbol_the_broker_does_not_carry_is_dropped():
    """Dropped rather than guessed at: proposing a code the broker rejects
    wastes the pass and the order."""
    db = ScreenDb(
        latest_ts=dt.datetime(2026, 9, 16, tzinfo=dt.timezone.utc),
        candles=[("OBSCURECO", Decimal("50"), 1_000_000)],
        isin_rows=[("OBSCURECO", "INE000X01011")],
        broker_rows=[],  # no Breeze row for that ISIN
    )
    assert await bhavcopy.screen(db, broker="icici_breeze", max_price=Decimal("2500")) == []


async def test_illiquid_stocks_are_excluded():
    """Liquidity matters more than price for a small account: a cheap share
    nobody trades cannot be exited at the screen price."""
    db = ScreenDb(
        latest_ts=dt.datetime(2026, 9, 16, tzinfo=dt.timezone.utc),
        candles=[("DEADCO", Decimal("20"), 100)],  # Rs 2,000 traded all day
        isin_rows=[("DEADCO", "INE000Y01011")],
        broker_rows=[("INE000Y01011", "DEADCO")],
    )
    found = await bhavcopy.screen(
        db, broker="icici_breeze", max_price=Decimal("2500")
    )
    assert found == []


async def test_no_bhavcopy_yet_returns_nothing_rather_than_failing():
    """A fresh deployment has no import until the nightly job has run. The
    caller falls back to pricing through the broker."""
    db = ScreenDb(latest_ts=None, candles=[], isin_rows=[], broker_rows=[])
    assert await bhavcopy.screen(db, broker="icici_breeze", max_price=Decimal("2500")) == []


# ── containment ──────────────────────────────────────────────────────


async def test_the_worker_job_never_raises(monkeypatch):
    """It shares a worker with order reconciliation. A failed download must
    not mark that worker unhealthy -- and nothing here is on the order path."""
    from app.workers import jobs

    async def explode(*a, **kw):
        raise RuntimeError("nsearchives unreachable")

    monkeypatch.setattr(jobs.bhavcopy, "import_day", explode)
    await jobs.bhavcopy_tick({})


async def test_a_holiday_is_information_not_an_error(monkeypatch):
    from app.workers import jobs

    async def unavailable(*a, **kw):
        raise bhavcopy.BhavcopyUnavailable("no bhavcopy published for 2026-10-02")

    monkeypatch.setattr(jobs.bhavcopy, "import_day", unavailable)
    await jobs.bhavcopy_tick({})


def test_the_import_is_not_reachable_from_the_live_price_path():
    """The safety property. Bhavcopy is a day old; it screens and backtests,
    and must never price an order. reference_price must not import it."""
    import inspect

    from app.services import quotes

    source = inspect.getsource(quotes)
    assert "bhavcopy" not in source
    assert bhavcopy.SOURCE not in source
