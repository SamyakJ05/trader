from datetime import datetime
from zoneinfo import ZoneInfo

from app.engines.risk.engine import is_market_open

IST = ZoneInfo("Asia/Kolkata")


def test_open_during_session():
    assert is_market_open(datetime(2026, 6, 10, 10, 30, tzinfo=IST))  # Wednesday


def test_closed_before_open():
    assert not is_market_open(datetime(2026, 6, 10, 9, 0, tzinfo=IST))


def test_closed_after_close():
    assert not is_market_open(datetime(2026, 6, 10, 15, 31, tzinfo=IST))


def test_closed_weekend():
    assert not is_market_open(datetime(2026, 6, 13, 10, 30, tzinfo=IST))  # Saturday


def test_boundary_open():
    assert is_market_open(datetime(2026, 6, 10, 9, 15, tzinfo=IST))
    assert is_market_open(datetime(2026, 6, 10, 15, 30, tzinfo=IST))
