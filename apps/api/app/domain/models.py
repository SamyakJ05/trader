from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.domain.enums import (
    Environment,
    Exchange,
    OptionRight,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    Validity,
)


class OrderRequest(BaseModel):
    """Broker-agnostic order intent. Adapters translate this into
    broker-specific payloads; nothing broker-specific belongs here."""

    symbol: str = Field(min_length=1, max_length=64)
    exchange: Exchange
    side: OrderSide
    order_type: OrderType
    product: ProductType
    quantity: int = Field(gt=0)
    price: Decimal | None = None
    trigger_price: Decimal | None = None
    validity: Validity = Validity.DAY

    # Derivatives contract identity. Absent for cash-segment orders, and
    # required by every broker for an F&O order: a symbol alone does not name
    # a contract, since the same underlying has many expiries and strikes.
    # Held as broker-neutral values (a date, a number, a right) that adapters
    # format themselves -- Breeze wants ISO 8601 and "call"/"put"/"others",
    # Kite encodes the whole contract into the tradingsymbol instead.
    expiry: date | None = None
    strike: Decimal | None = None
    right: OptionRight | None = None

    @model_validator(mode="after")
    def check_prices(self) -> "OrderRequest":
        if self.order_type in (OrderType.LIMIT, OrderType.SL) and self.price is None:
            raise ValueError(f"{self.order_type} order requires price")
        if self.order_type in (OrderType.SL, OrderType.SL_M) and self.trigger_price is None:
            raise ValueError(f"{self.order_type} order requires trigger_price")
        # An option needs both a strike and a right; a future needs neither.
        # Checked here so an incoherent contract cannot reach an adapter,
        # where it would become a broker rejection with a vaguer message.
        if self.right in (OptionRight.CALL, OptionRight.PUT):
            if self.strike is None:
                raise ValueError("an option order requires a strike")
            if self.expiry is None:
                raise ValueError("an option order requires an expiry")
        if self.strike is not None and self.right is None:
            raise ValueError("a strike without a right does not name a contract")
        return self


class PlaceOrderResult(BaseModel):
    broker_order_id: str | None = None
    status: OrderStatus
    status_message: str | None = None
    raw: dict = Field(default_factory=dict)


class BrokerProfile(BaseModel):
    broker_client_id: str
    name: str | None = None
    email: str | None = None
    raw: dict = Field(default_factory=dict)


class Funds(BaseModel):
    available_cash: Decimal
    margin_used: Decimal | None = None
    raw: dict = Field(default_factory=dict)


class Holding(BaseModel):
    symbol: str
    exchange: Exchange
    # The sellable figure, not the total on record. Brokers report several
    # quantities per holding -- total, pledged, blocked, T+1, free -- and the
    # difference is what can actually be sold today. Adapters must map the
    # free one here; `total_quantity` carries the rest of the picture.
    quantity: int
    total_quantity: int | None = None
    # None means the broker does not report a cost basis, which is the case
    # for ICICI Breeze's /dematholdings. Zero would be a lie that makes
    # unrealized P&L equal the full notional.
    average_price: Decimal | None = None
    last_price: Decimal | None = None
    pnl: Decimal | None = None


class BrokerPosition(BaseModel):
    symbol: str
    exchange: Exchange
    product: ProductType
    quantity: int
    average_price: Decimal
    last_price: Decimal | None = None
    realized_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None


class BrokerOrder(BaseModel):
    broker_order_id: str
    symbol: str
    exchange: Exchange
    side: OrderSide
    order_type: OrderType
    product: ProductType
    quantity: int
    filled_quantity: int = 0
    price: Decimal | None = None
    average_fill_price: Decimal | None = None
    status: OrderStatus
    status_message: str | None = None
    placed_at: datetime | None = None
    raw: dict = Field(default_factory=dict)


class Instrument(BaseModel):
    symbol: str
    exchange: Exchange
    broker_token: str | None = None
    name: str | None = None
    tick_size: Decimal | None = None
    lot_size: int | None = None
    instrument_type: str | None = None

    # Contract identity for derivatives; all None for a cash-segment row. A
    # symbol alone does not name an F&O contract -- Breeze lists 3,350 NIFTY
    # contracts under that one code -- so these are what distinguish them.
    expiry: date | None = None
    strike: Decimal | None = None
    option_right: OptionRight | None = None

    # The exchange's own identifier for the security, where the broker
    # publishes it. This is the only reliable bridge between brokers: each
    # uses private codes (Breeze's RELIND, Kite's RELIANCE) and an ISIN is
    # the same everywhere, so it is how a Breeze holding is matched to
    # imported market history.
    isin: str | None = None


class Tick(BaseModel):
    symbol: str
    exchange: Exchange
    last_price: Decimal
    ts: datetime


class RiskResult(BaseModel):
    decision: str  # RiskDecision
    reasons: list[str] = Field(default_factory=list)
    checked_rules: list[str] = Field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.decision == "ALLOW"


class EnvOrderContext(BaseModel):
    """Everything the order pipeline needs to know besides the OrderRequest."""

    environment: Environment
    client_order_id: str
    strategy_id: str | None = None
    last_price: Decimal | None = None
