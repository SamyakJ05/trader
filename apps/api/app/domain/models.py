from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.domain.enums import (
    Environment,
    Exchange,
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

    @model_validator(mode="after")
    def check_prices(self) -> "OrderRequest":
        if self.order_type in (OrderType.LIMIT, OrderType.SL) and self.price is None:
            raise ValueError(f"{self.order_type} order requires price")
        if self.order_type in (OrderType.SL, OrderType.SL_M) and self.trigger_price is None:
            raise ValueError(f"{self.order_type} order requires trigger_price")
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
    quantity: int
    average_price: Decimal
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
