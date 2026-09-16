from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from decimal import Decimal

from app.core.config import BrokerEnvCredentials
from app.db.models import BrokerAccount
from app.domain.capabilities import BrokerCapabilities, get_capabilities
from app.domain.enums import Broker, Exchange
from app.domain.models import (
    BrokerOrder,
    BrokerPosition,
    BrokerProfile,
    Funds,
    Holding,
    Instrument,
    OrderRequest,
    PlaceOrderResult,
    Tick,
)


class BrokerError(Exception):
    """Normalized broker failure. `retryable` hints at transient transport issues."""

    def __init__(self, message: str, *, retryable: bool = False, raw: dict | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.raw = raw or {}


class FeatureNotSupportedError(BrokerError):
    """Raised when a broker (or our adapter) does not support an operation.
    Check capabilities before calling instead of catching this."""


class SessionExpiredError(BrokerError):
    """Broker session is invalid/expired; user must re-authenticate."""


def as_int(value) -> int:
    """Parse a broker-supplied quantity that may not be an int.

    Brokers send quantities as strings, and not always clean ones: "1.0",
    "-" for an empty cell, "" for a missing value. int() raises on all
    three, and these parses live inside list comprehensions where one bad
    row would abort an entire holdings or positions fetch instead of
    skipping a line. Zero is the safe reading of an unparsable quantity --
    it under-reports rather than inventing stock that is not there.
    """
    if value in (None, "", "-"):
        return 0
    try:
        return int(Decimal(str(value)))
    except (ArithmeticError, ValueError):
        return 0


class BrokerAdapter(ABC):
    """Uniform interface every broker integration must implement.

    Adapters own ALL broker-specific concerns: auth/session lifecycle,
    payload shapes, enum mapping, rate-limit behavior. Nothing outside
    app/adapters/ may import broker SDKs or hit broker endpoints.
    """

    broker: Broker

    def __init__(self, account: BrokerAccount, credentials: BrokerEnvCredentials):
        self.account = account
        self.credentials = credentials

    @property
    def capabilities(self) -> BrokerCapabilities:
        return get_capabilities(self.broker)

    # ── session lifecycle ────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> dict:
        """Begin/establish a session. For redirect-based brokers returns
        {'login_url': ...}; for token-based brokers validates and returns
        {'status': 'connected'}."""

    @abstractmethod
    async def refresh_session(self) -> dict:
        """Re-validate or renew the session. Raises SessionExpiredError if
        the user must log in again."""

    # ── account data ─────────────────────────────────────────────────

    @abstractmethod
    async def get_profile(self) -> BrokerProfile: ...

    @abstractmethod
    async def get_funds(self) -> Funds: ...

    @abstractmethod
    async def get_holdings(self) -> list[Holding]: ...

    @abstractmethod
    async def get_positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    async def get_orders(self) -> list[BrokerOrder]: ...

    # ── trading ──────────────────────────────────────────────────────

    @abstractmethod
    async def place_order(self, request: OrderRequest, client_order_id: str) -> PlaceOrderResult: ...

    @abstractmethod
    async def modify_order(
        self, broker_order_id: str, request: OrderRequest
    ) -> PlaceOrderResult: ...

    @abstractmethod
    async def cancel_order(
        self, broker_order_id: str, exchange: Exchange | None = None
    ) -> PlaceOrderResult:
        """Cancel a resting order.

        `exchange` is optional because most brokers identify an order by id
        alone, but Breeze requires exchange_code on its cancel endpoint and an
        order id does not carry it. Adapters that do not need it ignore it.
        """
        ...

    # ── market data ──────────────────────────────────────────────────

    @abstractmethod
    async def get_instruments(self, exchange: str | None = None) -> list[Instrument]: ...

    @abstractmethod
    def subscribe_ticks(self, symbols: list[str]) -> AsyncIterator[Tick]:
        """Async iterator of normalized ticks. Adapters without streaming
        support raise FeatureNotSupportedError."""
