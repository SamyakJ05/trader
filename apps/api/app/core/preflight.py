"""Production configuration checks, run at startup.

Each of these is a setting whose wrong value is silent: the application starts,
every page renders, and the problem only appears as a security property that
was never actually holding. Logged loudly rather than raised, because refusing
to boot would take down a running instance over a misconfiguration that may be
deliberate -- except for the encryption key, which the code already refuses to
work without.
"""

from app.core.config import Settings, credential_ref_owners
from app.core.logging import get_logger

logger = get_logger(__name__)

DEV_ORIGINS = ("http://localhost:3000", "http://127.0.0.1:3000")


def check_production_config(settings: Settings) -> list[str]:
    """Return the problems found, having logged each. Empty means clean."""
    problems: list[str] = []

    def problem(event: str, detail: str) -> None:
        problems.append(detail)
        logger.error(event, detail=detail)

    if settings.debug:
        problem(
            "config_debug_enabled",
            "DEBUG is true: logs render in plain text at DEBUG level and may "
            "carry more detail than a production instance should keep.",
        )

    if not settings.app_encryption_key:
        problem(
            "config_no_encryption_key",
            "APP_ENCRYPTION_KEY is unset: TOTP secrets cannot be stored, and "
            "broker session tokens are held without encryption.",
        )

    dev_origins = [o for o in settings.cors_origins if o in DEV_ORIGINS]
    if dev_origins:
        problem(
            "config_dev_cors_origin",
            f"CORS_ORIGINS still allows {dev_origins}. Set it to the deployed "
            "origin only.",
        )

    if settings.enable_live_trading and not settings.broker_static_ip:
        # Live trading without a registered address to compare against means
        # the mismatch that rejects every order cannot be detected.
        problem(
            "config_live_without_static_ip",
            "ENABLE_LIVE_TRADING is on but BROKER_STATIC_IP is unset, so an "
            "outbound IP that no longer matches the broker's whitelist cannot "
            "be detected at startup.",
        )

    if not credential_ref_owners():
        # Not an error on a paper-only instance, which is the common case.
        logger.info(
            "config_no_credential_owners",
            detail="BROKER_CREDENTIAL_OWNERS is empty: no broker credential "
            "refs can be attached to any account.",
        )

    return problems
