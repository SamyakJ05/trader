"""The analyst's indicator tool.

The reason this exists rather than letting the model do arithmetic on a price
list: an LLM asked to compute a 14-period Wilder-smoothed RSI will produce a
confident number that is not one. Handing it the same implementation the
rule-based strategies trade on means a reading it quotes matches what they
would act on.
"""

import json

from app.services.ai.tools import TOOLS

BY_NAME = {t["name"]: t for t in TOOLS}


def test_the_tool_is_registered():
    assert "get_indicators" in BY_NAME


def test_it_requires_a_symbol():
    schema = BY_NAME["get_indicators"]["input_schema"]
    assert schema["required"] == ["symbol"]


def test_the_description_says_null_means_unknown_not_neutral():
    """The trap this tool has to close. RSI's neutral reading is 50, so a
    model that reads null as neutral concludes 'not overbought' from an
    absence of data and recommends a trade on nothing."""
    description = BY_NAME["get_indicators"]["description"]
    assert "null" in description
    assert "NOT neutral" in description


def test_the_description_points_the_model_away_from_raw_prices():
    """Without this the model keeps doing its own arithmetic on get_quotes
    output, which is where the wrong numbers come from."""
    assert "raw prices" in BY_NAME["get_indicators"]["description"]


async def test_thin_history_reports_nulls_rather_than_numbers(monkeypatch):
    """Too few candles must produce nulls, not values computed from whatever
    was available -- the model cannot tell a seed-only reading from a real
    one, and would size a proposal off it."""
    from types import SimpleNamespace

    from app.services.ai import tools as tools_module

    async def two_candles(db, symbol, exchange, interval, source, limit):
        from decimal import Decimal

        return [
            SimpleNamespace(
                open=Decimal(100),
                high=Decimal(101),
                low=Decimal(99),
                close=Decimal(100),
                volume=1,
            )
            for _ in range(2)
        ]

    monkeypatch.setattr(tools_module, "candle_history", two_candles)

    out = json.loads(
        await tools_module.run_tool(
            None,
            None,
            None,
            SimpleNamespace(environment="live", broker="icici_breeze", id=None),
            "get_indicators",
            {"symbol": "RELIANCE"},
        )
    )
    assert out["candles_available"] == 2
    assert out["rsi_14"] is None
    assert out["macd"] is None
    assert out["atr_14"] is None
    # The last close is still knowable from two candles and is reported.
    assert out["last_close"] == "100"
