"""Production config checks.

Each setting here is one whose wrong value is silent — the app starts and
everything renders while a security property quietly does not hold.
"""

from app.core.config import Settings
from app.core.preflight import check_production_config


def _settings(**overrides) -> Settings:
    base = {
        "debug": False,
        "app_encryption_key": "k" * 44,
        "cors_origins": ["https://tickortrade.online"],
        "enable_live_trading": False,
        "broker_static_ip": None,
    }
    base.update(overrides)
    return Settings(**base)


def test_clean_production_config_reports_nothing(monkeypatch):
    monkeypatch.setenv("BROKER_CREDENTIAL_OWNERS", "ICICI_MAIN:you@example.com")
    assert check_production_config(_settings()) == []


def test_debug_is_flagged():
    assert any("DEBUG" in p for p in check_production_config(_settings(debug=True)))


def test_missing_encryption_key_is_flagged():
    problems = check_production_config(_settings(app_encryption_key=None))
    assert any("APP_ENCRYPTION_KEY" in p for p in problems)


def test_localhost_cors_origin_is_flagged():
    problems = check_production_config(
        _settings(cors_origins=["https://tickortrade.online", "http://localhost:3000"])
    )
    assert any("CORS_ORIGINS" in p for p in problems)


def test_live_trading_without_a_registered_ip_is_flagged():
    """Without one, the mismatch that rejects every order is undetectable."""
    problems = check_production_config(
        _settings(enable_live_trading=True, broker_static_ip=None)
    )
    assert any("BROKER_STATIC_IP" in p for p in problems)


def test_live_trading_with_a_registered_ip_is_fine():
    problems = check_production_config(
        _settings(enable_live_trading=True, broker_static_ip="203.0.113.7")
    )
    assert not any("BROKER_STATIC_IP" in p for p in problems)


def test_empty_credential_owners_is_not_an_error(monkeypatch):
    """A paper-only instance is the common case, not a misconfiguration."""
    monkeypatch.delenv("BROKER_CREDENTIAL_OWNERS", raising=False)
    assert check_production_config(_settings()) == []
