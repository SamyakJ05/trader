"""Shared test configuration.

An APP_ENCRYPTION_KEY is set for the whole suite because TOTP secrets refuse
to be stored without one — the same condition that protects a real deployment
from writing second factors to the database in plaintext.
"""

import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True, scope="session")
def _encryption_key():
    from app.core.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    if not settings.app_encryption_key:
        settings.app_encryption_key = Fernet.generate_key().decode()
    yield
