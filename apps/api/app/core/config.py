import os
import re
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

    # Paper trading. Off means: no new paper accounts, existing ones accept no
    # orders, and their strategies are not evaluated -- the simulator becomes
    # read-only history rather than a place trades happen.
    #
    # The paper ENGINE stays in the codebase and stays imported: the backtester
    # depends on its charge model, its ledger arithmetic and its fill
    # accounting, so disabling the engine itself would take backtesting down
    # with it. This flag governs paper ACCOUNTS, not the machinery they share.
    #
    # Note this does not make live trading possible on its own. A live order
    # still needs ENABLE_LIVE_TRADING, the account's live_enabled, and an
    # adapter whose status is WORKING -- which no real broker has yet.
    enable_paper_trading: bool = True

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


# A credential ref names an env-var prefix, so it is interpolated into
# os.environ lookups. Anything a user can put here is therefore a read of the
# process environment, and the shape below is what keeps that from being an
# arbitrary one.
CREDENTIAL_REF_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_]{0,63}$")


def credential_ref_owners() -> dict[str, str]:
    """Which user owns each provisioned credential ref.

    Declared by the operator as BROKER_CREDENTIAL_OWNERS, a comma-separated
    list of REF:email pairs:

        BROKER_CREDENTIAL_OWNERS=ICICI_MAIN:you@example.com,ZERODHA_MAIN:you@example.com

    A ref not listed here cannot be attached to any account. The alternative --
    letting a user name any prefix -- means one tenant can name another's and
    get an adapter holding that tenant's API key and secret. The broker read
    paths are not behind the live gate, so that would be a live cross-user
    data breach rather than a theoretical one.
    """
    raw = os.environ.get("BROKER_CREDENTIAL_OWNERS", "")
    owners: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        ref, _, email = entry.partition(":")
        ref = ref.strip().upper()
        email = email.strip().lower()
        if ref and email and CREDENTIAL_REF_PATTERN.match(ref):
            owners[ref] = email
    return owners


class BrokerEnvCredentials:
    """Secrets are resolved from environment variables at call time, keyed by
    the broker account's credential_ref (e.g. ZERODHA_MAIN -> ZERODHA_MAIN_API_KEY).
    They are never persisted."""

    def __init__(self, ref: str):
        ref = ref.upper()
        # Refusing a malformed ref here as well as at the route is deliberate:
        # this constructor is reached from the adapter registry, postbacks and
        # the credential-status helper, and a value that predates validation
        # (or arrives from a future call site) must not become an env lookup.
        if ref and not CREDENTIAL_REF_PATTERN.match(ref):
            self.api_key = self.api_secret = self.access_token = None
            return
        self.api_key = os.environ.get(f"{ref}_API_KEY")
        self.api_secret = os.environ.get(f"{ref}_API_SECRET")
        self.access_token = os.environ.get(f"{ref}_ACCESS_TOKEN")

    @property
    def has_api_keys(self) -> bool:
        return bool(self.api_key and self.api_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
