import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Operator flag. Gates actions whose blast radius crosses tenants —
    # today only the global kill switch, which halts strategy execution for
    # every user on the instance.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # TOTP is mandatory: a user without totp_enabled_at can reach only the
    # enrolment endpoints. The secret is Fernet-encrypted at rest.
    totp_secret_enc: Mapped[str | None] = mapped_column(String(512))
    totp_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthSession(Base):
    """One row per signed-in device. Revocation is explicit (revoked_at) rather
    than a delete, so a revoked session stays visible in the device list and in
    the audit trail."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    ip: Mapped[str | None] = mapped_column(String(64))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # True for a session issued to finish TOTP enrolment. Its holder proved a
    # password but no second factor, so it authorises only the setup endpoints.
    enrolment_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    user: Mapped[User] = relationship()


class RecoveryCode(Base):
    """Single-use backup codes for TOTP. Mandatory 2FA without recovery makes a
    lost authenticator a manual-SQL lockout, so these are not optional.
    Stored bcrypt-hashed and shown to the user exactly once."""

    __tablename__ = "recovery_codes"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    code_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PasswordReset(Base):
    """Single-use password-reset token. Like invites, only the hash is stored;
    the raw token exists solely in the email."""

    __tablename__ = "password_resets"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Invite(Base):
    """Invite-only registration. The raw token exists only in the email we
    send; the database stores its hash, so a database read cannot be replayed
    into an account."""

    __tablename__ = "invites"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(255), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    full_name: Mapped[str | None] = mapped_column(String(255))
    as_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BrokerAccount(Base):
    __tablename__ = "broker_accounts"
    __table_args__ = (UniqueConstraint("user_id", "broker", "label"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    broker: Mapped[str] = mapped_column(String(32))
    environment: Mapped[str] = mapped_column(String(8), default="paper")
    label: Mapped[str] = mapped_column(String(64))
    # Env-var prefix used to resolve API key/secret; secrets never stored here.
    credential_ref: Mapped[str | None] = mapped_column(String(64))
    broker_client_id: Mapped[str | None] = mapped_column(String(64))
    # Short-lived broker session token, Fernet-encrypted (see core/security.py).
    session_token_enc: Mapped[str | None] = mapped_column(Text)
    session_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="disconnected")
    status_message: Mapped[str | None] = mapped_column(Text)
    live_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Lets the AI analyst place orders on this account with no human approval.
    # Off by default and per-account, so turning it on is a deliberate act for
    # one account rather than a mode the whole instance falls into. It does
    # not widen any other gate: a live account still needs ENABLE_LIVE_TRADING
    # and live_enabled, and every auto order goes through the same risk engine
    # as a manual one.
    auto_execute: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when profile+funds were last fetched successfully — read-path proof.
    read_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BrokerCapability(Base):
    __tablename__ = "broker_capabilities"
    __table_args__ = (UniqueConstraint("broker", "capability"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    broker: Mapped[str] = mapped_column(String(32), index=True)
    capability: Mapped[str] = mapped_column(String(64))
    supported: Mapped[bool] = mapped_column(Boolean)
    notes: Mapped[str | None] = mapped_column(Text)


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    broker_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(64))  # registry key, e.g. "sma_crossover"
    environment: Mapped[str] = mapped_column(String(8), default="paper")
    symbols: Mapped[list] = mapped_column(JSONB, default=list)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class StrategyRun(Base):
    __tablename__ = "strategy_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    strategy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="RUNNING")
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class TradingSignal(Base):
    __tablename__ = "trading_signals"

    id: Mapped[uuid.UUID] = uuid_pk()
    strategy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("strategy_runs.id", ondelete="SET NULL")
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    symbol: Mapped[str] = mapped_column(String(64))
    exchange: Mapped[str] = mapped_column(String(8))
    signal_type: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    acted: Mapped[bool] = mapped_column(Boolean, default=False)
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("broker_account_id", "client_order_id"),
        Index("ix_orders_user_env_status", "user_id", "environment", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="CASCADE")
    )
    strategy_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("strategies.id", ondelete="SET NULL")
    )
    environment: Mapped[str] = mapped_column(String(8))
    client_order_id: Mapped[str] = mapped_column(String(64))
    broker_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(64))
    exchange: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(8))
    product: Mapped[str] = mapped_column(String(8))
    validity: Mapped[str] = mapped_column(String(4), default="DAY")
    quantity: Mapped[int] = mapped_column(Integer)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    trigger_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    average_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    status: Mapped[str] = mapped_column(String(20), default="PENDING_RISK")
    status_message: Mapped[str | None] = mapped_column(Text)
    # True when the AI placed this without a human approving it. Recorded on
    # the order itself because it is the only durable answer to "did a person
    # agree to this trade?" -- the proposal it came from can be edited or
    # deleted, and an audit search cannot bound the daily auto-trade rule.
    auto_executed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # Which contract, for a derivatives order. modify_order rebuilds an
    # OrderRequest from this row, and Breeze requires all three on PUT /order
    # as well as POST -- so an F&O order that did not remember its contract
    # could be placed and then never amended.
    expiry: Mapped[date | None] = mapped_column(Date)
    strike: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    option_right: Mapped[str | None] = mapped_column(String(8))
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    broker_fill_id: Mapped[str | None] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    charges: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("broker_account_id", "symbol", "exchange", "product"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="CASCADE")
    )
    environment: Mapped[str] = mapped_column(String(8))
    symbol: Mapped[str] = mapped_column(String(64))
    exchange: Mapped[str] = mapped_column(String(8))
    product: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer, default=0)  # signed: + long, - short
    average_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class HoldingsSnapshot(Base):
    __tablename__ = "holdings_snapshots"

    id: Mapped[uuid.UUID] = uuid_pk()
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="CASCADE")
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    holdings: Mapped[list] = mapped_column(JSONB, default=list)


class FundsSnapshot(Base):
    __tablename__ = "funds_snapshots"

    id: Mapped[uuid.UUID] = uuid_pk()
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="CASCADE")
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    available_cash: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class DailyPnl(Base):
    """Per-day realized P&L, the figure MAX_DAILY_LOSS compares against.

    Postgres rather than Redis alone. The counter gates real money, and a
    Redis flush without persistence would reset it to zero mid-day — the rule
    would still evaluate, still pass, and stop protecting anything, which is
    the worst shape a safety control can fail in. Redis stays as the hot path;
    this is what it is rebuilt from.

    The day is an IST trading date, not a UTC one: a fill at 22:00 UTC belongs
    to the next morning's session in India.
    """

    __tablename__ = "daily_pnl"
    __table_args__ = (UniqueConstraint("user_id", "environment", "trading_day"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    environment: Mapped[str] = mapped_column(String(8))
    trading_day: Mapped[date] = mapped_column(Date)
    realized: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class MarketInstrument(Base):
    __tablename__ = "market_instruments"
    __table_args__ = (
        # A derivatives contract is NOT identified by its symbol: Breeze's F&O
        # master holds 79,612 contracts under 216 stock codes, NIFTY alone
        # having 3,350. Uniqueness is therefore on the whole contract, as a
        # functional index (see migration 0010) rather than a constraint,
        # because Postgres treats NULLs as distinct and equities carry NULL in
        # all three contract columns -- COALESCE sentinels keep equities
        # colliding on (broker, exchange, symbol) as before.
        Index(
            "uq_instruments_contract",
            "broker",
            "exchange",
            "symbol",
            text("COALESCE(expiry, DATE '1900-01-01')"),
            text("COALESCE(strike, -1)"),
            text("COALESCE(option_right, '')"),
            unique=True,
        ),
        Index("ix_instruments_symbol", "symbol", "exchange"),
        Index("ix_instruments_chain", "broker", "exchange", "symbol", "expiry"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    broker: Mapped[str] = mapped_column(String(32))
    broker_token: Mapped[str | None] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(255))
    exchange: Mapped[str] = mapped_column(String(8))
    segment: Mapped[str | None] = mapped_column(String(16))
    instrument_type: Mapped[str | None] = mapped_column(String(16))
    tick_size: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    lot_size: Mapped[int | None] = mapped_column(Integer)
    expiry: Mapped[date | None] = mapped_column(Date)
    strike: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    # "option_right", not "right": RIGHT is a reserved SQL keyword and would
    # need quoting in every hand-written query.
    option_right: Mapped[str | None] = mapped_column(String(8))
    # Exchange identifier, indexed because it is how one broker's instrument
    # is matched to another's -- or to imported history stored under an NSE
    # ticker the broker never uses.
    isin: Mapped[str | None] = mapped_column(String(16), index=True)


class RiskRule(Base):
    __tablename__ = "risk_rules"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    environment: Mapped[str] = mapped_column(String(8), default="paper")
    rule_type: Mapped[str] = mapped_column(String(32))
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    environment: Mapped[str] = mapped_column(String(8))
    rule_type: Mapped[str | None] = mapped_column(String(32))
    decision: Mapped[str] = mapped_column(String(8))
    reason: Mapped[str] = mapped_column(Text)
    context: Mapped[dict] = mapped_column(JSONB, default=dict)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    # bigserial gives a strict, gap-tolerant ordering for the event stream
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(64), index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    broker: Mapped[str] = mapped_column(String(32))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    headers: Mapped[dict] = mapped_column(JSONB, default=dict)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)


class AIProposal(Base):
    """Trade suggestion from the AI analyst. Never an order by itself —
    a human approval routes it through the standard order pipeline."""

    __tablename__ = "ai_proposals"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="CASCADE")
    )
    symbol: Mapped[str] = mapped_column(String(64))
    exchange: Mapped[str] = mapped_column(String(8), default="NSE")
    side: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(8), default="MARKET")
    product: Mapped[str] = mapped_column(String(8), default="MIS")
    quantity: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    # Contract, for a derivatives proposal. Without these an approved option
    # proposal would place a cash order in the underlying instead.
    expiry: Mapped[date | None] = mapped_column(Date)
    strike: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    option_right: Mapped[str | None] = mapped_column(String(8))
    rationale: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="PROPOSED", index=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AISettings(Base):
    """Per-user AI provider configuration. Credentials are a Fernet-encrypted
    JSON blob (see core/security.py) and are never returned by the API."""

    __tablename__ = "ai_settings"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    provider: Mapped[str] = mapped_column(String(16))  # anthropic|openai|openrouter|bedrock
    model: Mapped[str] = mapped_column(String(128))
    base_url: Mapped[str | None] = mapped_column(String(255))
    credentials_enc: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    response: Mapped[dict | None] = mapped_column(JSONB)


class CashLedger(Base):
    __tablename__ = "cash_ledger"
    __table_args__ = (
        Index("ix_cash_ledger_account_id", "broker_account_id", "id"),
        UniqueConstraint("fill_id", "entry_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    broker_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("broker_accounts.id"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    entry_type: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    fill_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("fills.id"))


class PaperHolding(Base):
    __tablename__ = "holdings"
    __table_args__ = (UniqueConstraint("broker_account_id", "symbol", "exchange"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    broker_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("broker_accounts.id"))
    symbol: Mapped[str] = mapped_column(String(64))
    exchange: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    average_price: Mapped[Decimal] = mapped_column(Numeric(18, 4))


class PendingSettlement(Base):
    __tablename__ = "pending_settlements"
    __table_args__ = (Index("ix_settlements_due", "settles_on", "broker_account_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    broker_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("broker_accounts.id"))
    fill_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("fills.id"), unique=True)
    symbol: Mapped[str] = mapped_column(String(64))
    exchange: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    settles_on: Mapped[date] = mapped_column(Date)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NewsItem(Base):
    """A corporate announcement or headline, for the analyst to read.

    Advisory only. Nothing here reaches a strategy or an order: an external
    feed is the one input an attacker can write to, and a feed that goes quiet
    is indistinguishable from a market with no news. Neither may move money on
    its own, so this informs proposals a human approves.

    url is unique: every source republishes, and the same filing arriving
    twice must not read as two events -- "three announcements today" is a
    signal an analyst would weigh.
    """

    __tablename__ = "news_items"
    __table_args__ = (
        UniqueConstraint("url", name="uq_news_url"),
        Index("ix_news_published", "published_at"),
        # The analyst asks "what is there about RELIANCE", so the lookup is
        # by symbol and recency together.
        Index("ix_news_symbol_published", "symbol", "published_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # Which feed it came from, so a misbehaving source can be identified and
    # its items discounted without dropping the table.
    source: Mapped[str] = mapped_column(String(32))
    # NULL when the item is market-wide rather than about one company. The
    # analyst reads those as context, not as a reason to trade a symbol.
    symbol: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (UniqueConstraint("symbol", "exchange", "interval", "source", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(64))
    exchange: Mapped[str] = mapped_column(String(8))
    interval: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(32))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    high: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    low: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    close: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    volume: Mapped[int | None] = mapped_column(BigInteger)
    # First/last observed tick timestamps preserve OHLC under out-of-order delivery.
    first_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    kind: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSONB)
    results: Mapped[dict] = mapped_column(JSONB)
