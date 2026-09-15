"""Daily broker session expiry.

Kite tokens die at the exchange flush, around 6am IST, whenever they were
issued. An account still showing CONNECTED after that is lying to the user:
every call will fail and strategies will throw on a schedule, with no
indication that a re-login is what's needed.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.domain.calendar import IST
from app.domain.enums import BrokerAccountStatus
from app.services import sessions_broker as bs


def account(broker="zerodha", token="enc:abc", expires_at=None, status="connected"):
    return SimpleNamespace(
        id="acct", user_id="user", broker=broker, session_token_enc=token,
        session_expires_at=expires_at, status=status, status_message=None,
    )


def ist(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


# ── when the flush happened ──────────────────────────────────────────


def test_after_the_morning_flush_the_boundary_is_today():
    assert bs.last_flush(ist(2026, 9, 15, 10, 0)) == ist(2026, 9, 15, 6, 0).astimezone(timezone.utc)


def test_before_the_morning_flush_the_boundary_is_yesterday():
    """At 3am the most recent flush was the previous morning, not one that has
    yet to happen."""
    assert bs.last_flush(ist(2026, 9, 15, 3, 0)) == ist(2026, 9, 14, 6, 0).astimezone(timezone.utc)


def test_the_flush_boundary_is_evaluated_in_ist():
    """01:00 UTC is 06:30 IST — just past the flush. Comparing in UTC would
    place it hours before."""
    utc_moment = datetime(2026, 9, 15, 1, 0, tzinfo=timezone.utc)
    assert bs.last_flush(utc_moment) == ist(2026, 9, 15, 6, 0).astimezone(timezone.utc)


# ── which sessions are stale ─────────────────────────────────────────


def test_a_token_with_a_past_expiry_is_stale():
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert bs.is_session_stale(account(expires_at=past))


def test_a_token_with_a_future_expiry_is_live():
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert not bs.is_session_stale(account(expires_at=future))


def test_a_token_with_no_expiry_is_treated_as_stale_after_a_flush():
    """Without a stored expiry we cannot tell when it was issued. Assuming it
    survived is the dangerous guess; a reconnect proves otherwise cheaply."""
    assert bs.is_session_stale(account(expires_at=None), now=ist(2026, 9, 15, 10, 0))


def test_an_account_with_no_token_is_not_stale():
    """Nothing to expire; it is simply not connected."""
    assert not bs.is_session_stale(account(token=None))


def test_paper_accounts_never_expire():
    """Paper has no broker session to lose."""
    past = datetime.now(timezone.utc) - timedelta(days=5)
    assert not bs.is_session_stale(account(broker="paper", expires_at=past))


def test_a_naive_expiry_is_read_as_utc():
    """Postgres hands back naive datetimes in some paths; comparing one to an
    aware 'now' would raise rather than expire the session."""
    naive_past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    assert bs.is_session_stale(account(expires_at=naive_past))


# ── the job ──────────────────────────────────────────────────────────


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return iter(self._rows)


class FakeDb:
    def __init__(self, rows):
        self._rows = rows
        self.commits = 0

    async def execute(self, *args, **kwargs):
        return FakeResult(self._rows)

    def add(self, obj):
        pass

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


async def test_stale_accounts_are_marked_expired():
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    stale = account(expires_at=past)
    assert await bs.expire_stale_sessions(FakeDb([stale])) == 1
    assert stale.status == BrokerAccountStatus.SESSION_EXPIRED.value
    assert "reconnect" in stale.status_message.lower()


async def test_live_accounts_are_left_alone():
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    live = account(expires_at=future)
    assert await bs.expire_stale_sessions(FakeDb([live])) == 0
    assert live.status == "connected"


async def test_one_bad_account_does_not_stop_the_others():
    """Same reasoning as the paper tick: this runs for every user."""
    past = datetime.now(timezone.utc) - timedelta(hours=1)

    class Exploding(SimpleNamespace):
        @property
        def status_message(self):
            return None

        @status_message.setter
        def status_message(self, value):
            raise RuntimeError("write failed")

    broken = Exploding(
        id="bad", user_id="u", broker="zerodha", session_token_enc="enc:x",
        session_expires_at=past, status="connected",
    )
    healthy = account(expires_at=past)
    assert await bs.expire_stale_sessions(FakeDb([broken, healthy])) == 1
    assert healthy.status == BrokerAccountStatus.SESSION_EXPIRED.value


# ── brokers disagree about when sessions die ─────────────────────────


def test_breeze_sessions_die_at_midnight_not_at_kites_flush():
    """Breeze expires at midnight IST; Kite at ~06:00. Using Kite's time for
    Breeze would leave an account claiming to be connected for six hours after
    its session was already dead."""
    three_am = ist(2026, 9, 15, 3, 0)
    assert bs.last_flush(three_am, broker="icici_breeze") == ist(
        2026, 9, 15, 0, 0
    ).astimezone(timezone.utc)
    assert bs.last_flush(three_am, broker="zerodha") == ist(
        2026, 9, 14, 6, 0
    ).astimezone(timezone.utc)


def test_a_breeze_session_from_yesterday_is_stale_after_midnight():
    stale = account(broker="icici_breeze", expires_at=None)
    assert bs.is_session_stale(stale, now=ist(2026, 9, 15, 3, 0))


def test_an_unknown_broker_gets_the_later_flush():
    """Kite's 06:00 is the conservative default: it expires sessions later, so
    an unknown broker is not marked dead while it might still be alive."""
    assert bs.last_flush(ist(2026, 9, 15, 3, 0), broker="something-new") == bs.last_flush(
        ist(2026, 9, 15, 3, 0), broker="zerodha"
    )


async def test_breeze_accounts_are_expired_by_the_job():
    """Breeze was absent from the flush list entirely, so its sessions were
    never marked expired however old they were."""
    from datetime import timedelta

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    stale = account(broker="icici_breeze", expires_at=past)
    assert await bs.expire_stale_sessions(FakeDb([stale])) == 1
    assert stale.status == BrokerAccountStatus.SESSION_EXPIRED.value
