"""NSE equity trading holidays.

A data file rather than a lookup: the exchange publishes the list annually and
it is small. Weekends are handled separately — only weekday closures belong
here, so a holiday that falls on a Saturday or Sunday is recorded in the
comments rather than the tuple.

MAINTENANCE: update each year when NSE publishes the next calendar.
`tests/test_calendar.py` fails when the current or next year is missing, so a
stale list is loud rather than silently treating a holiday as a trading day.

Sources: NSE's published holiday calendar, corroborated across Zerodha, Groww,
Tata MF and Jiraaf reproductions (2026: four sources in agreement; 2025:
three). The authoritative artifacts are the NSE circulars on nseindia.com,
which rate-limit automated fetches — worth verifying once from a browser
before this drives real settlement.
"""

from datetime import date

NSE_HOLIDAYS: dict[int, tuple[date, ...]] = {
    2025: (
        date(2025, 2, 26),   # Mahashivratri
        date(2025, 3, 14),   # Holi
        date(2025, 3, 31),   # Id-Ul-Fitr (Ramzan Id)
        date(2025, 4, 10),   # Shri Mahavir Jayanti
        date(2025, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
        date(2025, 4, 18),   # Good Friday
        date(2025, 5, 1),    # Maharashtra Day
        date(2025, 8, 15),   # Independence Day
        date(2025, 8, 27),   # Ganesh Chaturthi
        date(2025, 10, 2),   # Mahatma Gandhi Jayanti / Dussehra (same day)
        date(2025, 10, 21),  # Diwali Laxmi Pujan (Muhurat session that evening)
        date(2025, 10, 22),  # Diwali Balipratipada
        date(2025, 11, 5),   # Prakash Gurpurb Sri Guru Nanak Dev
        date(2025, 12, 25),  # Christmas
    ),
    # Fell on weekends in 2025, so no separate closure: 26 Jan (Sun, Republic
    # Day), 6 Apr (Sun, Ram Navami), 7 Jun (Sat, Bakri Id), 6 Jul (Sun,
    # Muharram).
    2026: (
        date(2026, 1, 15),   # Municipal Corporation Elections - Maharashtra
        date(2026, 1, 26),   # Republic Day
        date(2026, 3, 3),    # Holi
        date(2026, 3, 26),   # Shri Ram Navami
        date(2026, 3, 31),   # Shri Mahavir Jayanti
        date(2026, 4, 3),    # Good Friday
        date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
        date(2026, 5, 1),    # Maharashtra Day
        date(2026, 5, 28),   # Bakri Id
        date(2026, 6, 26),   # Muharram
        date(2026, 9, 14),   # Ganesh Chaturthi
        date(2026, 10, 2),   # Mahatma Gandhi Jayanti
        date(2026, 10, 20),  # Dussehra
        date(2026, 11, 10),  # Diwali Balipratipada
        date(2026, 11, 24),  # Prakash Gurpurb Sri Guru Nanak Dev
        date(2026, 12, 25),  # Christmas
    ),
    # Fell on weekends in 2026: 15 Feb (Sun, Mahashivratri), 21 Mar (Sat,
    # Id-Ul-Fitr), 15 Aug (Sat, Independence Day), 8 Nov (Sun, Diwali Laxmi
    # Pujan - Muhurat session held that Sunday).
    #
    # 2027 IS NOT YET PUBLISHED. T+1 settlement in late December 2026 reaches
    # into 2027, so add it as soon as NSE releases the circular; until then
    # the calendar raises CalendarUnavailable rather than guessing.
}
