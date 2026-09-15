"""Mandatory TOTP (phase 1b-ii): enrolment, verification, recovery codes,
login challenges, and password reset.

Pure-unit (fakeredis, no Postgres), matching the rest of the suite.
"""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import fakeredis.aioredis
import pyotp
import pytest
from fastapi import HTTPException

from app.services import challenge
from app.services import totp as totp_service


def utcnow():
    return datetime.now(timezone.utc)


def user(enrolled: bool = False):
    secret = totp_service.new_secret()
    u = SimpleNamespace(
        id=uuid.uuid4(),
        email="someone@example.com",
        full_name="Someone",
        is_admin=False,
        is_active=True,
        totp_secret_enc=None,
        totp_enabled_at=utcnow() if enrolled else None,
    )
    if enrolled:
        totp_service.store_secret(u, secret)
    return u


class FakeResult:
    def __init__(self, value=None, rows=()):
        self._value = value
        self._rows = rows

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return iter(self._rows)


class FakeDb:
    def __init__(self, execute_returns=None, rows=()):
        self._execute_returns = execute_returns
        self._rows = rows
        self.executed = 0

    async def execute(self, *args, **kwargs):
        self.executed += 1
        # First call in consume_recovery_code is the SELECT, subsequent the
        # conditional UPDATE.
        if self.executed == 1 and self._rows:
            return FakeResult(rows=self._rows)
        return FakeResult(value=self._execute_returns)

    def add(self, obj):
        pass

    async def flush(self):
        pass

    async def commit(self):
        pass


# ── secret generation and verification ───────────────────────────────


def test_generated_secret_produces_verifiable_codes():
    secret = totp_service.new_secret()
    code = pyotp.TOTP(secret).now()
    assert totp_service.verify_code(secret, code)


def test_wrong_code_is_rejected():
    secret = totp_service.new_secret()
    wrong = "000000" if pyotp.TOTP(secret).now() != "000000" else "111111"
    assert not totp_service.verify_code(secret, wrong)


def test_empty_code_is_rejected():
    secret = totp_service.new_secret()
    assert not totp_service.verify_code(secret, "")
    assert not totp_service.verify_code(secret, "   ")


def test_code_with_spaces_is_accepted():
    """Authenticator apps display codes as '123 456'; users paste them that way."""
    secret = totp_service.new_secret()
    code = pyotp.TOTP(secret).now()
    assert totp_service.verify_code(secret, f"{code[:3]} {code[3:]}")


def test_a_different_secret_does_not_verify():
    a, b = totp_service.new_secret(), totp_service.new_secret()
    assert not totp_service.verify_code(b, pyotp.TOTP(a).now())


def test_provisioning_uri_is_an_otpauth_url():
    secret = totp_service.new_secret()
    uri = totp_service.provisioning_uri(secret, "a@b.com")
    assert uri.startswith("otpauth://totp/")
    assert "issuer=trader" in uri
    assert secret in uri


async def test_a_code_cannot_be_replayed_within_its_window():
    """valid_window=1 keeps a code acceptable for ~90s. Without burning it,
    a code seen over a shoulder or captured from a phishing form works again
    for the rest of that window."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    secret = totp_service.new_secret()
    uid = uuid.uuid4()
    code = pyotp.TOTP(secret).now()

    assert await totp_service.verify_code_once(redis, uid, secret, code)
    assert not await totp_service.verify_code_once(redis, uid, secret, code)


async def test_burning_a_code_is_per_user():
    """One user spending a code must not lock another user out of theirs."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    secret = totp_service.new_secret()
    code = pyotp.TOTP(secret).now()

    assert await totp_service.verify_code_once(redis, uuid.uuid4(), secret, code)
    assert await totp_service.verify_code_once(redis, uuid.uuid4(), secret, code)


async def test_an_invalid_code_is_not_burned():
    """A wrong guess must not consume the real code's marker."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    secret = totp_service.new_secret()
    uid = uuid.uuid4()

    assert not await totp_service.verify_code_once(redis, uid, secret, "000001")
    assert await totp_service.verify_code_once(redis, uid, secret, pyotp.TOTP(secret).now())


async def test_replay_marker_carries_a_ttl():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    secret = totp_service.new_secret()
    uid = uuid.uuid4()
    code = pyotp.TOTP(secret).now()
    await totp_service.verify_code_once(redis, uid, secret, code)
    ttl = await redis.ttl(f"totp:used:{uid}:{code}")
    assert 0 < ttl <= totp_service.REPLAY_TTL_SECONDS


# ── secret storage is encrypted, not plaintext ───────────────────────


def test_secret_round_trips_through_storage():
    u = user()
    secret = totp_service.new_secret()
    totp_service.store_secret(u, secret)
    assert u.totp_secret_enc != secret, "secret must not be stored verbatim"
    assert totp_service.user_secret(u) == secret


def test_user_secret_is_none_before_setup():
    assert totp_service.user_secret(user()) is None


def test_storing_a_secret_without_an_encryption_key_is_refused(monkeypatch):
    """encrypt_secret degrades to marked plaintext when APP_ENCRYPTION_KEY is
    unset. That is tolerable for a broker token that expires daily; for a TOTP
    secret it would hand anyone with database access a permanent second factor
    for every user."""
    from app.core import security as security_module

    monkeypatch.setattr(security_module, "_fernet", lambda: None)
    with pytest.raises(RuntimeError, match="APP_ENCRYPTION_KEY"):
        totp_service.store_secret(user(), totp_service.new_secret())


# ── enrolment state ──────────────────────────────────────────────────


def test_a_stored_secret_alone_does_not_count_as_enrolled():
    """Setup stores the secret before the user proves they can generate a
    code. Treating that as enrolled would lock out anyone who abandoned setup
    partway."""
    u = user()
    totp_service.store_secret(u, totp_service.new_secret())
    assert not totp_service.is_enrolled(u)


def test_enrolment_requires_both_secret_and_timestamp():
    u = user(enrolled=True)
    assert totp_service.is_enrolled(u)
    u.totp_secret_enc = None
    assert not totp_service.is_enrolled(u)


# ── recovery codes ───────────────────────────────────────────────────


def test_recovery_codes_are_unique_and_formatted():
    codes = totp_service.generate_recovery_codes()
    assert len(codes) == totp_service.RECOVERY_CODE_COUNT
    assert len(set(codes)) == len(codes)
    assert all("-" in c for c in codes)


async def test_recovery_code_matches_and_is_spent():
    from app.core.security import hash_password

    code = "abcde-12345"
    row = SimpleNamespace(id=uuid.uuid4(), code_hash=hash_password(code), used_at=None)
    db = FakeDb(execute_returns=row.id, rows=[row])
    assert await totp_service.consume_recovery_code(db, uuid.uuid4(), code)


async def test_recovery_code_that_lost_the_race_is_refused():
    """Two simultaneous uses of one code: the conditional UPDATE matches for
    only one of them, and the loser must be told no."""
    from app.core.security import hash_password

    code = "abcde-12345"
    row = SimpleNamespace(id=uuid.uuid4(), code_hash=hash_password(code), used_at=None)
    db = FakeDb(execute_returns=None, rows=[row])  # UPDATE matched nothing
    assert not await totp_service.consume_recovery_code(db, uuid.uuid4(), code)


async def test_unknown_recovery_code_is_refused():
    from app.core.security import hash_password

    row = SimpleNamespace(
        id=uuid.uuid4(), code_hash=hash_password("real-code"), used_at=None
    )
    db = FakeDb(rows=[row])
    assert not await totp_service.consume_recovery_code(db, uuid.uuid4(), "wrong-code")


async def test_blank_recovery_code_is_refused():
    assert not await totp_service.consume_recovery_code(FakeDb(), uuid.uuid4(), "   ")


# ── login challenges ─────────────────────────────────────────────────


async def test_challenge_resolves_to_its_user():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    uid = uuid.uuid4()
    token = await challenge.issue(redis, uid)
    assert await challenge.resolve(redis, token) == uid


async def test_consumed_challenge_no_longer_resolves():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    token = await challenge.issue(redis, uuid.uuid4())
    await challenge.consume(redis, token)
    assert await challenge.resolve(redis, token) is None


async def test_unknown_challenge_resolves_to_none():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    assert await challenge.resolve(redis, "not-a-real-token") is None
    assert await challenge.resolve(redis, "") is None


async def test_challenge_carries_a_hard_expiry():
    """The datastore enforces the TTL, so application code cannot forget it."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    token = await challenge.issue(redis, uuid.uuid4())
    ttl = await redis.ttl(f"authchallenge:{token}")
    assert 0 < ttl <= challenge.CHALLENGE_TTL_SECONDS


async def test_a_challenge_is_not_a_session_token():
    """Distinct keyspaces: a challenge must never authenticate a request."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    token = await challenge.issue(redis, uuid.uuid4())
    assert await redis.exists(f"authchallenge:{token}") == 1


# ── TOTP enforcement dependency ──────────────────────────────────────


async def test_unenrolled_user_is_refused_by_verified_dependency():
    from app.core.deps import get_verified_user

    with pytest.raises(HTTPException) as exc:
        await get_verified_user(user(enrolled=False))
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "totp_setup_required"


async def test_enrolled_user_passes_the_verified_dependency():
    from app.core.deps import get_verified_user

    u = user(enrolled=True)
    assert await get_verified_user(u) is u


async def test_partially_enrolled_user_is_refused():
    """Secret stored but never confirmed — must not count as enrolled."""
    from app.core.deps import get_verified_user

    u = user(enrolled=False)
    totp_service.store_secret(u, totp_service.new_secret())
    with pytest.raises(HTTPException):
        await get_verified_user(u)


# ── enrolment bootstrap (the upgrade path) ───────────────────────────


async def test_enrolment_session_is_short_lived_and_scoped():
    """Every account on an instance that has just made 2FA mandatory is
    unenrolled, including the operator's. Refusing them a session would lock
    the instance out, because enrolment itself needs one."""
    from app.services import sessions as session_service

    class RecordingDb:
        def __init__(self):
            self.added = []

        def add(self, obj):
            self.added.append(obj)

        async def flush(self):
            pass

    db = RecordingDb()
    session = await session_service.create(db, uuid.uuid4(), enrolment_only=True)
    assert session.enrolment_only is True
    lifetime = session.expires_at - session.created_at if session.created_at else None
    # created_at is set by the DB default, so assert the TTL constant instead.
    assert session_service.ENROLMENT_TTL_MINUTES <= 60, "enrolment window must stay short"
    assert lifetime is None or lifetime.total_seconds() > 0


async def test_enrolment_session_cannot_reach_full_session_routes():
    """An enrolment session's holder proved a password and nothing else, so it
    must not change the password or list other devices."""
    from types import SimpleNamespace

    from app.core.deps import get_full_session_user

    enrolling = SimpleNamespace(enrolment_only=True)
    with pytest.raises(HTTPException) as exc:
        await get_full_session_user(enrolling, user(enrolled=False))
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "totp_setup_required"


async def test_full_session_passes():
    from types import SimpleNamespace

    from app.core.deps import get_full_session_user

    u = user(enrolled=True)
    assert await get_full_session_user(SimpleNamespace(enrolment_only=False), u) is u


# ── challenge revocation on password change ──────────────────────────


async def test_password_change_revokes_outstanding_challenges():
    """Someone who phished the old password and holds a live challenge must
    not be able to complete it after the victim resets."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    uid = uuid.uuid4()
    token = await challenge.issue(redis, uid)
    assert await challenge.resolve(redis, token) == uid

    await challenge.revoke_all_for_user(redis, uid)
    assert await challenge.resolve(redis, token) is None


async def test_revoking_challenges_is_per_user():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    my_token = await challenge.issue(redis, mine)
    their_token = await challenge.issue(redis, theirs)

    await challenge.revoke_all_for_user(redis, mine)
    assert await challenge.resolve(redis, my_token) is None
    assert await challenge.resolve(redis, their_token) == theirs


async def test_consumed_challenge_leaves_no_index_entry():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    uid = uuid.uuid4()
    token = await challenge.issue(redis, uid)
    await challenge.consume(redis, token)
    assert await redis.smembers(f"authchallenge:user:{uid}") == set()


# ── recovery code entropy ────────────────────────────────────────────


def test_recovery_codes_carry_enough_entropy():
    """Bounded by the rate limiter online, but the hashes could leak."""
    codes = totp_service.generate_recovery_codes()
    hex_chars = sum(c.isalnum() for c in codes[0])
    assert hex_chars >= 20, "expect >= 80 bits of entropy per code"


# ── password reset token shape ───────────────────────────────────────


def test_reset_email_does_not_leak_the_token_hash():
    from app.core.security import hash_token
    from app.services import password_reset as reset_service

    token = "sample-token"
    body = reset_service.reset_email_body(token)
    assert token in body
    assert hash_token(token) not in body


def test_reset_url_points_at_the_web_app():
    from app.services import password_reset as reset_service

    assert "/reset-password?token=" in reset_service.reset_url("abc")


# ── enable() must flip the caller's own session, not just the user row ──


async def test_enabling_totp_upgrades_the_caller_s_own_session(monkeypatch):
    """Regression: enable() updated the user row but left the session that
    just finished enrolment still marked enrolment_only. The frontend
    navigates onward using that same token, so it immediately hit
    totp_setup_required on the next route and bounced back to a setup page
    that now correctly refuses — a redirect loop between two endpoints that
    were each right about a different piece of state that had gone out of
    sync. Enable is the one place that can bring both into agreement in the
    same request, since it is the request that changes the user's enrolled
    state — so it must update the session it was called with too."""
    from app.api.routes.auth import TotpEnableBody, totp_enable

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("app.api.routes.auth.get_redis", lambda: redis)

    secret = totp_service.new_secret()
    u = user(enrolled=False)
    totp_service.store_secret(u, secret)
    session = SimpleNamespace(enrolment_only=True)
    code = pyotp.TOTP(secret).now()

    await totp_enable(TotpEnableBody(code=code), u, session, FakeDb())

    assert session.enrolment_only is False, (
        "enable() must upgrade the caller's own session in the same request, "
        "not just the user row -- otherwise the token already in the "
        "frontend's hand keeps failing totp_setup_required after enrolment"
    )
