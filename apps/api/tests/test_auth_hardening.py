"""Auth hardening (phase 1b-i): invite-only registration, session/device
management, login rate limiting, and admin self-lockout guards.

Pure-unit (fakeredis, no Postgres), matching the rest of the suite.
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from fastapi import HTTPException

from app.core.security import hash_token, new_invite_token
from app.services import invites as invite_service
from app.services import ratelimit


def utcnow():
    return datetime.now(timezone.utc)


class FakeResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeDb:
    """Stands in for AsyncSession. `execute_returns` is what the atomic
    UPDATE ... RETURNING yields: a row when the statement matched, None when
    its WHERE clause refused."""

    def __init__(self, execute_returns=None):
        self._execute_returns = execute_returns

    async def execute(self, *args, **kwargs):
        return FakeResult(self._execute_returns)


def user(is_admin: bool = False, is_active: bool = True):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="someone@example.com",
        full_name="Someone",
        is_admin=is_admin,
        is_active=is_active,
    )


def invite_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        email="invitee@example.com",
        token_hash="",
        invited_by=uuid.uuid4(),
        full_name=None,
        as_admin=False,
        created_at=utcnow(),
        expires_at=utcnow() + timedelta(days=7),
        consumed_at=None,
        revoked_at=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ── invite validity ──────────────────────────────────────────────────


def test_invite_token_is_stored_only_as_a_hash():
    token = new_invite_token()
    stored = hash_token(token)
    assert token not in stored
    assert stored == hash_token(token), "hash must be deterministic for lookup"


def test_valid_invite_is_accepted():
    assert invite_service.invite_error(invite_row()) is None


def test_consumed_invite_is_refused():
    row = invite_row(consumed_at=utcnow())
    assert "already been used" in invite_service.invite_error(row).lower()


def test_expired_invite_is_refused():
    row = invite_row(expires_at=utcnow() - timedelta(minutes=1))
    assert "expired" in invite_service.invite_error(row).lower()


def test_revoked_invite_is_refused():
    row = invite_row(revoked_at=utcnow())
    assert "revoked" in invite_service.invite_error(row).lower()


def test_missing_invite_is_refused():
    assert "not valid" in invite_service.invite_error(None).lower()


async def test_consume_returns_the_row_it_claimed():
    """Consumption is a single UPDATE ... WHERE ... RETURNING: a returned row
    means this caller won the claim."""
    claimed = invite_row()
    result = await invite_service.consume(FakeDb(execute_returns=claimed), "tok")
    assert result is claimed


async def test_consume_returns_none_when_the_row_was_not_claimable():
    """Zero rows returned is the refusal — consumed, revoked, expired, or
    already claimed by a concurrent registration. The database decides, so two
    simultaneous submissions of one token cannot both succeed."""
    assert await invite_service.consume(FakeDb(execute_returns=None), "tok") is None


# ── rate limiting ────────────────────────────────────────────────────


async def test_rate_limiter_allows_under_the_threshold():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    for _ in range(4):
        await ratelimit.record_failure(redis, "login", "a@b.com")
    await ratelimit.check(redis, "login", "a@b.com")  # must not raise


async def test_rate_limiter_trips_at_the_threshold():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    for _ in range(5):
        await ratelimit.record_failure(redis, "login", "a@b.com")
    with pytest.raises(ratelimit.RateLimitExceeded) as exc:
        await ratelimit.check(redis, "login", "a@b.com")
    assert exc.value.retry_after > 0


async def test_rate_limiter_is_per_identifier():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    for _ in range(5):
        await ratelimit.record_failure(redis, "login", "victim@b.com")
    await ratelimit.check(redis, "login", "bystander@b.com")  # unaffected


async def test_successful_login_clears_the_counter():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    for _ in range(4):
        await ratelimit.record_failure(redis, "login", "a@b.com")
    await ratelimit.clear(redis, "login", "a@b.com")
    for _ in range(4):
        await ratelimit.record_failure(redis, "login", "a@b.com")
    await ratelimit.check(redis, "login", "a@b.com")  # budget was reset


async def test_rate_limit_is_case_insensitive_on_email():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    for _ in range(5):
        await ratelimit.record_failure(redis, "login", "Victim@B.com")
    with pytest.raises(ratelimit.RateLimitExceeded):
        await ratelimit.check(redis, "login", "victim@b.com")


# ── admin self-lockout guards ────────────────────────────────────────


def test_admin_cannot_suspend_themselves():
    from app.api.routes.admin import _guard_self

    me = user(is_admin=True)
    with pytest.raises(HTTPException) as exc:
        _guard_self(me, me, "suspend")
    assert exc.value.status_code == 409
    assert "yourself" in exc.value.detail.lower()


def test_admin_cannot_demote_themselves():
    from app.api.routes.admin import _guard_self

    me = user(is_admin=True)
    with pytest.raises(HTTPException) as exc:
        _guard_self(me, me, "demote")
    assert "yourself" in exc.value.detail.lower()


def test_acting_on_another_user_passes_the_self_guard():
    from app.api.routes.admin import _guard_self

    _guard_self(user(is_admin=True), user(is_admin=True), "demote")


async def test_demote_refuses_when_no_other_admin_remains():
    """The last-admin rule lives inside the UPDATE, not in Python: two
    operators demoting each other at once would both pass a Python-side
    count and leave the instance with zero admins."""
    from app.services import admin as admin_service

    # Zero rows updated => the subquery found no other active admin.
    assert not await admin_service.demote(FakeDb(execute_returns=None), uuid.uuid4())


async def test_demote_succeeds_when_another_admin_remains():
    from app.services import admin as admin_service

    assert await admin_service.demote(FakeDb(execute_returns=uuid.uuid4()), uuid.uuid4())


async def test_suspend_refuses_when_no_other_admin_remains():
    from app.services import admin as admin_service

    assert not await admin_service.suspend(FakeDb(execute_returns=None), uuid.uuid4())


async def test_suspend_succeeds_for_an_ordinary_user():
    from app.services import admin as admin_service

    assert await admin_service.suspend(FakeDb(execute_returns=uuid.uuid4()), uuid.uuid4())


# ── email backend selection ──────────────────────────────────────────


async def test_console_sender_is_used_in_debug_without_a_provider_key(monkeypatch):
    from app.services import email as email_module

    monkeypatch.setattr(
        email_module,
        "get_settings",
        lambda: SimpleNamespace(resend_api_key=None, email_from=None, debug=True),
    )
    assert isinstance(email_module.get_sender(), email_module.ConsoleSender)


async def test_missing_provider_outside_debug_fails_loudly(monkeypatch):
    """The console backend logs raw invite tokens. Silently falling back to it
    in production would publish account-creation links to the application log,
    so a missing key must raise instead."""
    from app.services import email as email_module

    monkeypatch.setattr(
        email_module,
        "get_settings",
        lambda: SimpleNamespace(resend_api_key=None, email_from=None, debug=False),
    )
    with pytest.raises(email_module.EmailNotConfigured):
        email_module.get_sender()


async def test_send_reports_failure_instead_of_raising_when_unconfigured(monkeypatch):
    from app.services import email as email_module

    monkeypatch.setattr(
        email_module,
        "get_settings",
        lambda: SimpleNamespace(resend_api_key=None, email_from=None, debug=False),
    )
    assert await email_module.send("a@b.com", "s", "b") is False


async def test_resend_sender_is_used_when_configured(monkeypatch):
    from app.services import email as email_module

    monkeypatch.setattr(
        email_module,
        "get_settings",
        lambda: SimpleNamespace(resend_api_key="re_test", email_from="a@b.com", debug=False),
    )
    assert isinstance(email_module.get_sender(), email_module.ResendSender)


async def test_send_swallows_provider_errors(monkeypatch):
    """A bounced invite must not fail the request that created it: the invite
    row is valid either way, and the operator can pass the link on by hand."""
    from app.services import email as email_module

    class Boom:
        async def send(self, message):
            raise RuntimeError("provider down")

    monkeypatch.setattr(email_module, "get_sender", Boom)
    assert await email_module.send("a@b.com", "s", "b") is False


# ── session revocation semantics ─────────────────────────────────────


def test_session_is_live_only_when_unrevoked_and_unexpired():
    from app.services.sessions import is_live

    assert is_live(SimpleNamespace(revoked_at=None, expires_at=utcnow() + timedelta(hours=1)))
    assert not is_live(
        SimpleNamespace(revoked_at=utcnow(), expires_at=utcnow() + timedelta(hours=1))
    )
    assert not is_live(
        SimpleNamespace(revoked_at=None, expires_at=utcnow() - timedelta(seconds=1))
    )
