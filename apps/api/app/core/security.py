import secrets

import bcrypt
from cryptography.fernet import Fernet

from app.core.config import get_settings

_PLAIN_PREFIX = "plain:"
_ENC_PREFIX = "enc:"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(48)


def _fernet() -> Fernet | None:
    key = get_settings().app_encryption_key
    return Fernet(key.encode()) if key else None


def encrypt_secret(value: str) -> str:
    """Encrypt broker session tokens at rest. Falls back to a marked plaintext
    value when APP_ENCRYPTION_KEY is unset (local dev only)."""
    f = _fernet()
    if f is None:
        return _PLAIN_PREFIX + value
    return _ENC_PREFIX + f.encrypt(value.encode()).decode()


def decrypt_secret(stored: str) -> str:
    if stored.startswith(_PLAIN_PREFIX):
        return stored[len(_PLAIN_PREFIX):]
    if stored.startswith(_ENC_PREFIX):
        f = _fernet()
        if f is None:
            raise RuntimeError("APP_ENCRYPTION_KEY required to decrypt stored token")
        return f.decrypt(stored[len(_ENC_PREFIX):].encode()).decode()
    return stored


def new_invite_token() -> str:
    """Raw invite/reset token. Emailed once; only its hash is stored."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 for single-use link tokens.

    Deliberately not bcrypt: these are high-entropy random tokens looked up on
    every click, not user-chosen passwords, so a slow KDF buys nothing and a
    fast digest lets the lookup be a plain indexed query.
    """
    import hashlib

    return hashlib.sha256(token.encode()).hexdigest()
