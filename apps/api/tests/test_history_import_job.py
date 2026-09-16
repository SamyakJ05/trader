"""Importing history from the UI.

A backtest cannot invent bars it does not have, so until history exists for a
symbol the strategy that trades it cannot be tested at all. That made
importing a prerequisite for the feature meant to tell you whether a strategy
is worth running -- and it was reachable only by shell.

No network: the Yahoo download is always replaced.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.workers import jobs


class FakeDb:
    def __init__(self):
        self.committed = 0
        self.rolled_back = 0

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        self.rolled_back += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def patch_import(monkeypatch, *, results=None, failures=()):
    """Replace the per-symbol import with a recorded outcome."""
    seen = []

    async def fake(db, *, symbol, interval, start, end):
        seen.append(symbol)
        if symbol in failures:
            raise ValueError(f"no data found for {symbol}")
        return (results or {}).get(symbol, {"symbol": symbol, "candles": 250})

    import app.services.history as history_module

    monkeypatch.setattr(history_module, "import_symbol", fake)
    monkeypatch.setattr(jobs, "async_session_factory", lambda: FakeDb())
    return seen


async def test_each_symbol_is_imported(monkeypatch):
    seen = patch_import(monkeypatch)
    result = await jobs.import_history_job(
        {}, user_id="u", symbols=["RELIANCE", "INFY"], interval="1d",
        start="2025-01-01", end="2026-01-01",
    )
    assert seen == ["RELIANCE", "INFY"]
    assert len(result["imported"]) == 2
    assert result["failed"] == []


async def test_one_bad_symbol_does_not_discard_the_others(monkeypatch):
    """A delisting, a typo, or a range Yahoo has no data for must not throw
    away the symbols that imported cleanly -- the operator would have no way
    to tell which ones landed."""
    seen = patch_import(monkeypatch, failures={"DELISTED"})
    result = await jobs.import_history_job(
        {}, user_id="u", symbols=["RELIANCE", "DELISTED", "INFY"], interval="1d",
        start="2025-01-01", end="2026-01-01",
    )
    assert seen == ["RELIANCE", "DELISTED", "INFY"]
    assert [r["symbol"] for r in result["imported"]] == ["RELIANCE", "INFY"]
    assert result["failed"][0]["symbol"] == "DELISTED"
    assert "no data found" in result["failed"][0]["error"]


async def test_a_failure_reports_why(monkeypatch):
    """Yahoo's messages are usually actionable, and they are the operator's
    only clue about a job nobody watched run."""
    patch_import(monkeypatch, failures={"NOPE"})
    result = await jobs.import_history_job(
        {}, user_id="u", symbols=["NOPE"], interval="1d",
        start="2025-01-01", end="2026-01-01",
    )
    assert result["imported"] == []
    assert result["failed"][0]["error"]


# ── the report the UI shows ──────────────────────────────────────────


async def test_split_suspects_reach_the_caller(monkeypatch):
    """Yahoo's unadjusted data records a split as a genuine overnight
    collapse, and a backtest spanning that date reads it as a price move.
    Surfacing it when the data lands is what stops an inexplicable equity
    curve being discovered weeks later."""
    import app.services.history as history_module

    rows = [
        {"ts": datetime(2026, 1, 1, tzinfo=timezone.utc), "open": Decimal("100"),
         "high": Decimal("101"), "low": Decimal("99"), "close": Decimal("100"),
         "volume": 1000},
        {"ts": datetime(2026, 1, 2, tzinfo=timezone.utc), "open": Decimal("50"),
         "high": Decimal("51"), "low": Decimal("49"), "close": Decimal("50"),
         "volume": 1000},
    ]

    async def fake_to_thread(fn, *a, **kw):
        return "frame"

    monkeypatch.setattr(history_module, "normalize_frame", lambda frame, interval: rows)

    async def fake_import_rows(db, symbol, interval, rows_):
        return None

    monkeypatch.setattr(history_module, "import_rows", fake_import_rows)

    import asyncio

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    import sys
    import types

    sys.modules.setdefault("yfinance", types.SimpleNamespace(download=lambda *a, **k: None))

    report = await history_module.import_symbol(
        FakeDb(), symbol="SPLITCO", interval="1d",
        start=date(2026, 1, 1), end=date(2026, 1, 3),
    )
    assert report["candles"] == 2
    assert report["split_suspects"], "a 50% overnight drop was not flagged"
    assert report["split_suspects"][0]["likely_split"] == "2:1"


def test_the_symbol_cap_is_stated():
    """Each symbol is a separate slow download. A request naming fifty would
    run for many minutes with no progress visible."""
    from app.api.routes.backtests import ImportHistoryBody

    field = ImportHistoryBody.model_fields["symbols"]
    assert any(getattr(m, "max_length", None) == 10 for m in field.metadata)


def test_symbols_are_normalised_and_a_bad_range_is_refused():
    import pydantic

    from app.api.routes.backtests import ImportHistoryBody

    body = ImportHistoryBody(
        symbols=[" reliance ", "infy"], start=date(2025, 1, 1), end=date(2026, 1, 1)
    )
    assert body.symbols == ["RELIANCE", "INFY"]
    with pytest.raises(pydantic.ValidationError):
        ImportHistoryBody(
            symbols=["RELIANCE"], start=date(2026, 1, 1), end=date(2025, 1, 1)
        )


async def test_a_missing_dependency_is_reported_not_crashed(monkeypatch):
    """yfinance is an optional extra. If the deployed image ever lacks it
    again, the job must report the reason per symbol rather than raising out
    of the worker, where nothing would tell the operator why every import
    silently stopped working."""
    import app.services.history as history_module

    async def explode(db, *, symbol, interval, start, end):
        raise ModuleNotFoundError("No module named 'yfinance'")

    monkeypatch.setattr(history_module, "import_symbol", explode)
    monkeypatch.setattr(jobs, "async_session_factory", lambda: FakeDb())

    result = await jobs.import_history_job(
        {}, user_id="u", symbols=["RELIANCE"], interval="1d",
        start="2025-01-01", end="2026-01-01",
    )
    assert result["imported"] == []
    assert "yfinance" in result["failed"][0]["error"]


def test_the_image_installs_the_history_extra():
    """The import runs in the worker now, so the deployed image needs
    yfinance -- which pyproject declares as an optional extra, not a base
    dependency."""
    from pathlib import Path

    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    assert '".[history]"' in dockerfile.read_text()
