"""NSE trading calendar.

Answers two questions the platform keeps asking: is the market open now, and
which day does a trade settle on. Both need the holiday list, which is why
they live together.
"""

from datetime import date, datetime, time, timedelta, timezone

from app.domain.data.nse_holidays import NSE_HOLIDAYS

IST = timezone(timedelta(hours=5, minutes=30))
NSE_OPEN = time(9, 15)
NSE_CLOSE = time(15, 30)


class CalendarUnavailable(RuntimeError):
    """Raised when asked about a year the holiday list does not cover.

    Deliberately loud: silently treating an unknown year as holiday-free would
    settle trades on Diwali and let orders through on Republic Day.
    """


def known_years() -> tuple[int, ...]:
    return tuple(sorted(NSE_HOLIDAYS))


def holidays(year: int) -> tuple[date, ...]:
    if year not in NSE_HOLIDAYS:
        raise CalendarUnavailable(
            f"No NSE holiday list for {year}. Update "
            "app/domain/data/nse_holidays.py from the exchange circular."
        )
    return NSE_HOLIDAYS[year]


def is_trading_day(day: date) -> bool:
    if day.weekday() >= 5:
        return False
    return day not in holidays(day.year)


def next_trading_day(day: date, *, offset: int = 1) -> date:
    """The trading day `offset` sessions after `day`.

    offset=1 from a Friday is the following Monday, or Tuesday if Monday is a
    holiday. This is what T+1 settlement means: one *session*, not one day.
    """
    if offset < 1:
        raise ValueError("offset must be at least 1")
    current = day
    remaining = offset
    while remaining:
        current += timedelta(days=1)
        if is_trading_day(current):
            remaining -= 1
    return current


def settlement_date(trade_day: date) -> date:
    """T+1 settlement for equity delivery."""
    return next_trading_day(trade_day, offset=1)


def is_market_open(now: datetime | None = None) -> bool:
    """NSE equity cash market hours: 09:15-15:30 IST on a trading day."""
    moment = (now or datetime.now(timezone.utc)).astimezone(IST)
    if not is_trading_day(moment.date()):
        return False
    return NSE_OPEN <= moment.time() <= NSE_CLOSE
