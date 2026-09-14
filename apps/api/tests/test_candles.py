from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.engines.market.candles import bucket_start, rollup


def test_boundary_belongs_to_new_candle():
    assert bucket_start(datetime(2026, 9, 15, 4, 0, tzinfo=timezone.utc), "1m").minute == 0
    assert bucket_start(datetime(2026, 9, 15, 3, 59, 59, tzinfo=timezone.utc), "1m").minute == 59


def test_rollup_requires_all_five_minutes():
    from types import SimpleNamespace

    rows = [
        SimpleNamespace(
            ts=datetime(2026, 9, 15, 4, i, tzinfo=timezone.utc),
            open=Decimal(100 + i),
            high=Decimal(110 + i),
            low=Decimal(90 + i),
            close=Decimal(101 + i),
            volume=None,
        )
        for i in range(5)
    ]
    result = rollup(rows)
    assert (result["open"], result["high"], result["low"], result["close"]) == (100, 114, 90, 105)
    assert result["volume"] is None  # Price-only ticks do not invent volume.
    assert rollup(rows[:4]) is None
    assert rollup([rows[0], rows[0], *rows[2:]]) is None


def test_naive_timestamp_refused():
    with pytest.raises(ValueError, match="timezone"):
        bucket_start(datetime(2026, 9, 15), "1m")
