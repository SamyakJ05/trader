import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "trader-api"
    debug: bool = False

    database_url: str = "postgresql+asyncpg://trader:trader@localhost:5432/trader"
    redis_url: str = "redis://localhost:6379/0"

    # Connection budget, not a performance dial. Managed Postgres caps
    # connections per cluster while this application opens pools from every
    # api worker and the arq worker, so the total has to stay under that cap.
    # See app/db/session.py for the arithmetic.
    db_pool_size: int = 3
    db_max_overflow: int = 2
    db_pool_recycle_seconds: int = 1800
    db_pool_timeout_seconds: int = 30

    app_secret_key: str = "dev-secret-change-me"
    app_encryption_key: str | None = None
    session_ttl_hours: int = 168

    enable_live_trading: bool = False

    # The outbound IP registered with the broker for transactional requests.
    # Logged and compared at startup so a mismatch surfaces before an order
    # is rejected for it. Purely diagnostic — it gates nothing.
    broker_static_ip: str | None = None
    market_hours_enforced: bool = True

    # AI trading. Env key is the zero-config fallback; per-user provider
    # settings (anthropic/openai/openrouter/bedrock) live in ai_settings.
    anthropic_api_key: str | None = None
    ai_model: str = "claude-opus-5"

    # Outbound email. Without a key the console backend logs messages instead,
    # so invite and reset links stay usable in development.
    resend_api_key: str | None = None
    email_from: str | None = None
    invite_ttl_hours: int = 168

    zerodha_redirect_url: str = "http://localhost:8000/api/v1/brokers/zerodha/callback"
    web_base_url: str = "http://localhost:3000"
    cors_origins: list[str] = ["http://localhost:3000"]


class BrokerEnvCredentials:
    """Secrets are resolved from environment variables at call time, keyed by
    the broker account's credential_ref (e.g. ZERODHA_MAIN -> ZERODHA_MAIN_API_KEY).
    They are never persisted."""

    def __init__(self, ref: str):
        ref = ref.upper()
        self.api_key = os.environ.get(f"{ref}_API_KEY")
        self.api_secret = os.environ.get(f"{ref}_API_SECRET")
        self.access_token = os.environ.get(f"{ref}_ACCESS_TOKEN")

    @property
    def has_api_keys(self) -> bool:
        return bool(self.api_key and self.api_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
