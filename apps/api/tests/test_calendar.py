"""NSE trading calendar: market hours and T+1 settlement.

The calendar decides when orders are allowed and when delivery trades settle,
so a wrong answer here is a wrong answer everywhere downstream.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.domain import calendar as cal


def ist(y, m, d, hh=10, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=cal.IST)


# ── the list must be maintained ──────────────────────────────────────


def test_current_year_is_covered():
    """Fails loudly when the holiday list goes stale rather than letting an
    uncovered year be treated as holiday-free."""
    this_year = datetime.now(timezone.utc).year
    assert this_year in cal.known_years(), (
        f"No NSE holiday list for {this_year}. Update "
        "app/domain/data/nse_holidays.py from the exchange circular."
    )


def test_settlement_late_in_the_year_fails_loudly_without_next_year():
    """T+1 in late December reaches into the following year. NSE publishes its
    calendar only a few months ahead, so the next year is often genuinely
    unavailable — the requirement is that this raises rather than silently
    treating an unknown year as holiday-free."""
    last_session = date(2026, 12, 31)
    if (last_session.year + 1) in cal.known_years():
        assert cal.is_trading_day(cal.settlement_date(last_session))
        return
    with pytest.raises(cal.CalendarUnavailable):
        cal.settlement_date(last_session)


def test_an_uncovered_year_raises_rather_than_guessing():
    with pytest.raises(cal.CalendarUnavailable):
        cal.holidays(1999)


# ── trading days ─────────────────────────────────────────────────────


def test_weekends_are_not_trading_days():
    saturday = date(2026, 9, 12)
    sunday = date(2026, 9, 13)
    assert saturday.weekday() == 5 and sunday.weekday() == 6
    assert not cal.is_trading_day(saturday)
    assert not cal.is_trading_day(sunday)


def test_a_listed_holiday_is_not_a_trading_day():
    for holiday in cal.holidays(2026):
        assert not cal.is_trading_day(holiday), f"{holiday} is listed but reported open"


def test_an_ordinary_weekday_is_a_trading_day():
    day = date(2026, 9, 16)  # Wednesday, not in the holiday list
    assert day.weekday() < 5 and day not in cal.holidays(2026)
    assert cal.is_trading_day(day)


# ── settlement ───────────────────────────────────────────────────────


def test_settlement_skips_the_weekend():
    friday = date(2026, 6, 5)
    assert friday.weekday() == 4
    assert cal.is_trading_day(friday)
    settles = cal.settlement_date(friday)
    assert settles == date(2026, 6, 8), "Friday trade settles the following Monday"
    assert settles.weekday() == 0


def test_settlement_skips_a_weekend_and_a_monday_holiday_together():
    """Friday 11 Sep 2026 -> Monday 14th is Ganesh Chaturthi -> Tuesday 15th.
    T+1 counts sessions, so two non-trading days in a row both get skipped."""
    friday = date(2026, 9, 11)
    assert cal.is_trading_day(friday)
    assert not cal.is_trading_day(date(2026, 9, 14))
    assert cal.settlement_date(friday) == date(2026, 9, 15)


def test_settlement_skips_a_holiday():
    """T+1 counts sessions, not calendar days: a holiday pushes it out."""
    holiday = cal.holidays(2026)[0]
    day_before = holiday - timedelta(days=1)
    if not cal.is_trading_day(day_before):
        pytest.skip("holiday follows a non-trading day; covered by the weekend case")
    settles = cal.settlement_date(day_before)
    assert settles != holiday
    assert cal.is_trading_day(settles)


def test_settlement_always_lands_on_a_trading_day():
    day = date(2026, 1, 1)
    while day < date(2026, 12, 20):
        if cal.is_trading_day(day):
            assert cal.is_trading_day(cal.settlement_date(day))
        day += timedelta(days=1)


def test_settlement_is_strictly_after_the_trade():
    day = date(2026, 6, 1)
    assert cal.settlement_date(day) > day


def test_offset_must_be_positive():
    with pytest.raises(ValueError):
        cal.next_trading_day(date(2026, 6, 1), offset=0)


# ── market hours ─────────────────────────────────────────────────────


def test_market_is_open_during_the_session():
    assert cal.is_market_open(ist(2026, 9, 16, 10, 0))


def test_market_is_closed_before_open_and_after_close():
    assert not cal.is_market_open(ist(2026, 9, 16, 9, 14))
    assert not cal.is_market_open(ist(2026, 9, 16, 15, 31))


def test_session_boundaries_are_inclusive():
    assert cal.is_market_open(ist(2026, 9, 16, 9, 15))
    assert cal.is_market_open(ist(2026, 9, 16, 15, 30))


def test_market_is_closed_on_a_holiday_during_session_hours():
    holiday = cal.holidays(2026)[0]
    moment = datetime(holiday.year, holiday.month, holiday.day, 11, 0, tzinfo=cal.IST)
    assert not cal.is_market_open(moment)


def test_market_hours_are_evaluated_in_ist_not_utc():
    """04:00 UTC is 09:30 IST — inside the session. A naive UTC comparison
    would call this closed."""
    utc_moment = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)
    assert cal.is_market_open(utc_moment)
