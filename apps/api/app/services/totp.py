"""TOTP second factor and single-use recovery codes.

TOTP is mandatory on this instance: these accounts place orders against real
broker connections, and a password alone is a single point of failure. A user
without an enabled secret can reach only the enrolment endpoints.

Because it is mandatory, recovery codes are not optional — without them a lost
authenticator is a manual-SQL lockout. Codes are bcrypt-hashed (unlike invite
tokens, they are short enough for offline guessing to matter) and shown once.
"""

import secrets
import uuid
from datetime import datetime, timezone

import pyotp
import redis.asyncio as aioredis
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret, encrypt_secret, hash_password, verify_password
from app.db.models import RecoveryCode, User

ISSUER = "trader"
RECOVERY_CODE_COUNT = 10
# One step of leeway each way: phone clocks drift, and 30s of tolerance is the
# usual trade for not rejecting honest codes.
VALID_WINDOW = 1
# Must outlive the acceptance window (±1 step around a 30s step = ~90s).
REPLAY_TTL_SECONDS = 120


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str) -> str:
    """otpauth:// URI for the QR code shown during enrolment."""
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def verify_code(secret: str, code: str) -> bool:
    """Stateless check. Callers on the login path should use
    `verify_code_once` instead, which also prevents replay."""
    if not code or not code.strip():
        return False
    return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=VALID_WINDOW)


async def verify_code_once(
    redis: aioredis.Redis, user_id: uuid.UUID, secret: str, code: str
) -> bool:
    """Verify and burn a TOTP code.

    valid_window=1 means a code stays acceptable for roughly 90 seconds, so
    without this a code observed over someone's shoulder — or captured from a
    phished form — can be replayed for the rest of that window. Marking it used
    in Redis, with a TTL covering the window, makes each code good exactly once.

    SET NX is the atomic part: two concurrent submissions of the same code race
    for the marker and only one wins.
    """
    if not verify_code(secret, code):
        return False
    normalised = code.strip().replace(" ", "")
    key = f"totp:used:{user_id}:{normalised}"
    claimed = await redis.set(key, "1", ex=REPLAY_TTL_SECONDS, nx=True)
    return bool(claimed)


def user_secret(user: User) -> str | None:
    if not user.totp_secret_enc:
        return None
    return decrypt_secret(user.totp_secret_enc)


def store_secret(user: User, secret: str) -> None:
    """Persist the TOTP secret encrypted.

    encrypt_secret falls back to marked plaintext when APP_ENCRYPTION_KEY is
    unset, which is tolerable for a broker session token that expires daily but
    not for a TOTP secret: anyone who can read the database would hold a
    working second factor for every user, permanently, which defeats the point
    of requiring one. Refuse rather than store it recoverably.
    """
    stored = encrypt_secret(secret)
    if not stored.startswith("enc:"):
        raise RuntimeError(
            "APP_ENCRYPTION_KEY must be set before enabling two-factor "
            "authentication — a TOTP secret must never be stored in plaintext."
        )
    user.totp_secret_enc = stored


def is_enrolled(user: User) -> bool:
    return user.totp_enabled_at is not None and bool(user.totp_secret_enc)


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Human-transcribable codes: lowercase base32-ish, grouped for reading."""
    codes = []
    for _ in range(count):
        raw = secrets.token_hex(10)  # 80 bits
        codes.append(f"{raw[:5]}-{raw[5:10]}-{raw[10:15]}-{raw[15:]}")
    return codes


async def replace_recovery_codes(
    db: AsyncSession, user_id: uuid.UUID, codes: list[str]
) -> None:
    """Issuing a new set invalidates the old one — otherwise a code printed
    before a 2FA reset would still work after it."""
    await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
    for code in codes:
        db.add(RecoveryCode(user_id=user_id, code_hash=hash_password(code)))


async def consume_recovery_code(
    db: AsyncSession, user_id: uuid.UUID, code: str
) -> bool:
    """Spends one unused code. Returns False if none match.

    The match is a bcrypt compare per stored code, so this is deliberately not
    a lookup — there is no way to index a hash whose salt differs per row. Ten
    comparisons is acceptable on a login path that is already rate limited.
    """
    candidate = code.strip().lower().replace(" ", "")
    if not candidate:
        return False
    result = await db.execute(
        select(RecoveryCode).where(
            RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None)
        )
    )
    for row in result.scalars():
        if verify_password(candidate, row.code_hash):
            # Conditional update: two simultaneous uses of the same code cannot
            # both succeed, since only one will match used_at IS NULL.
            spent = await db.execute(
                update(RecoveryCode)
                .where(RecoveryCode.id == row.id, RecoveryCode.used_at.is_(None))
                .values(used_at=utcnow())
                .returning(RecoveryCode.id)
            )
            return spent.scalar_one_or_none() is not None
    return False


async def remaining_recovery_codes(db: AsyncSession, user_id: uuid.UUID) -> int:
    result = await db.execute(
        select(RecoveryCode).where(
            RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None)
        )
    )
    return len(list(result.scalars()))


async def disable(db: AsyncSession, user: User) -> None:
    """Clears the second factor and every outstanding recovery code, forcing
    re-enrolment. Used by the operator 'reset 2FA' action."""
    user.totp_secret_enc = None
    user.totp_enabled_at = None
    await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
