"""Backtesting a strategy that already exists.

The only way to run a backtest was to retype an SMA configuration by hand,
so the strategy you actually intended to trade was never the thing tested.
Worse for Breeze: its symbols are ICICI's own codes (RELIND) while imported
history is keyed by the NSE ticker (RELIANCE), so a Breeze strategy matched
no candles at all and could not be backtested even by hand.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.services import instruments as instrument_service


class FakeResult:
    def __init__(self, value=None, rows=()):
        self._value = value
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(first=lambda: (self._rows[0] if self._rows else None))


class FakeDb:
    """Answers the two queries history_symbol makes, in order: this broker's
    ISIN for the symbol, then another broker's symbol for that ISIN."""

    def __init__(self, isin=None, match=None):
        self._responses = [FakeResult(value=isin), FakeResult(rows=[match] if match else [])]

    async def execute(self, *args, **kwargs):
        return self._responses.pop(0) if self._responses else FakeResult()


# ── symbol resolution, which is what makes Breeze backtestable ───────


async def test_a_breeze_code_resolves_to_the_ticker_history_uses():
    """RELIND and RELIANCE are the same security. The ISIN is the only
    reliable bridge: each broker's codes are private, an ISIN is not."""
    db = FakeDb(isin="INE002A01018", match="RELIANCE")
    resolved = await instrument_service.history_symbol(
        db, broker="icici_breeze", symbol="RELIND"
    )
    assert resolved == "RELIANCE"


async def test_a_symbol_with_no_isin_is_passed_through_unchanged():
    """Kite's codes ARE NSE tickers, so there is nothing to translate.
    Refusing here would break the broker that needs no help."""
    db = FakeDb(isin=None)
    resolved = await instrument_service.history_symbol(
        db, broker="zerodha", symbol="RELIANCE"
    )
    assert resolved == "RELIANCE"


async def test_an_isin_with_no_counterpart_falls_back_to_the_original():
    """Better to run against the symbol as given -- and find no candles, with
    a message saying so -- than to silently backtest a different instrument."""
    db = FakeDb(isin="INE002A01018", match=None)
    resolved = await instrument_service.history_symbol(
        db, broker="icici_breeze", symbol="RELIND"
    )
    assert resolved == "RELIND"


# ── what the route refuses ───────────────────────────────────────────


async def test_an_ai_strategy_cannot_be_replayed(monkeypatch):
    """Its decisions depended on context that no longer exists and were never
    recorded, so a replay would be inventing them. Refused before any candle
    is fetched, with a message saying what to do instead."""
    from fastapi import HTTPException

    from app.api.routes import backtests

    strategy = SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), kind="ai_agent",
        symbols=["RELIANCE"], params={}, broker_account_id=None,
        name="llm thing",
    )

    class OneRow:
        async def execute(self, *a, **kw):
            return FakeResult(value=strategy)

        async def get(self, model, pk):
            return None

    body = SimpleNamespace(
        strategy_id=strategy.id, symbol=None, interval="1d",
        source="yfinance_unadjusted", start=None, end=None, initial_cash=None,
    )
    with pytest.raises(HTTPException) as excinfo:
        await backtests.backtest_strategy(
            body, SimpleNamespace(id=strategy.user_id), OneRow()
        )
    assert excinfo.value.status_code == 422
    assert "cannot be replayed" in excinfo.value.detail


async def test_a_symbol_outside_the_strategy_is_refused():
    """Backtesting a symbol the strategy does not trade would produce a
    result that describes nothing the strategy would ever do."""
    from fastapi import HTTPException

    from app.api.routes import backtests

    strategy = SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), kind="sma_crossover",
        symbols=["RELIANCE"], params={}, broker_account_id=None, name="sma",
    )

    class OneRow:
        async def execute(self, *a, **kw):
            return FakeResult(value=strategy)

        async def get(self, model, pk):
            return None

    body = SimpleNamespace(
        strategy_id=strategy.id, symbol="INFY", interval="1d",
        source="yfinance_unadjusted", start=None, end=None, initial_cash=None,
    )
    with pytest.raises(HTTPException) as excinfo:
        await backtests.backtest_strategy(
            body, SimpleNamespace(id=strategy.user_id), OneRow()
        )
    assert excinfo.value.status_code == 422
    assert "not one of this strategy" in excinfo.value.detail


def test_both_routes_share_one_replay_path():
    """A strategy-driven run and a hand-configured one must price and score
    identically, or comparing them means nothing."""
    import inspect

    from app.api.routes import backtests

    for handler in (backtests.create_backtest, backtests.backtest_strategy):
        assert "_run(" in inspect.getsource(handler)
