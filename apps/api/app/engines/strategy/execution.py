"""Turning a strategy's decision into an order the broker will accept.

A signal says what to do; this says how to express it to a particular broker.
The two are separate because brokers differ in ways a strategy should not have
to know: Breeze accepts no market orders at all and no intraday cash product,
while Kite accepts both. A strategy written against one would otherwise be
silently wrong on the other.

The runner previously hardcoded `order_type=MARKET` and defaulted the product
to MIS. For Breeze that is a guaranteed double rejection -- its API takes only
limit and stoploss, and it has no MIS equivalent -- so every signal a Breeze
strategy ever produced would have failed at the adapter, after passing risk
checks and being recorded as an order.

Precedence, most specific first:

  1. what the signal asked for      (this strategy, this trade)
  2. what the strategy's params say  (this strategy, every trade)
  3. what the broker can accept      (the floor)

A strategy that says nothing gets something sane. A strategy that asks for
something the broker cannot do is refused loudly rather than quietly given a
different order.
"""

from decimal import ROUND_DOWN, ROUND_UP, Decimal

from app.adapters.base import FeatureNotSupportedError
from app.core.logging import get_logger
from app.domain.enums import Broker, Exchange, OrderSide, OrderType, ProductType, Validity
from app.domain.models import OrderRequest

logger = get_logger(__name__)

# Brokers that cannot express a market order. For these, a signal asking for
# one is converted to a marketable limit -- priced through the last trade so
# it crosses the spread and fills -- rather than rejected, because "buy now"
# is a coherent instruction that this broker simply spells differently.
#
# The adapter itself still refuses to make this substitution, and should: down
# there the caller's intent is already lost. Here the intent is explicit and
# the strategy can say how aggressive to be.
_NO_MARKET_ORDER = {Broker.ICICI_BREEZE.value}

# What each broker's cash-equity strategies default to when nothing says
# otherwise. Breeze has no MIS equivalent, so CNC (delivery) is the only
# honest default: it is what the account already holds, it needs no leverage,
# and it is not force-squared-off at 3:20pm.
_DEFAULT_PRODUCT = {
    Broker.ICICI_BREEZE.value: ProductType.CNC,
    Broker.ZERODHA.value: ProductType.MIS,
    Broker.GROWW.value: ProductType.MIS,
    Broker.PAPER.value: ProductType.MIS,
}

# Used when a market order must become a limit and nobody said how far
# through the price to reach. 0.30% fills reliably on liquid large caps
# without giving away much: the order normally executes at the spread, so
# this is a cap on slippage rather than a price paid.
DEFAULT_LIMIT_BUFFER_PCT = Decimal("0.003")

# The most a strategy may reach through the book, however aggressive it
# claims to be. A strategy asking to pay 10% through the last trade has a
# bug -- a units error, a bad LLM completion -- and executing a bug is worse
# than refusing it. Chosen to sit above any legitimate spread on a liquid
# instrument and well below a damaging one.
MAX_LIMIT_BUFFER_PCT = Decimal("0.05")

# Indian equities quote in paise.
_TICK = Decimal("0.05")


def _round_to_tick(price: Decimal, *, side: OrderSide) -> Decimal:
    """Round to a price the exchange will accept.

    Away from the touch in the direction that still fills: a buy rounds up
    and a sell rounds down, so rounding never makes a marketable order
    unmarketable by a paisa.
    """
    steps = price / _TICK
    rounded = steps.to_integral_value(
        rounding=ROUND_UP if side == OrderSide.BUY else ROUND_DOWN
    )
    return (rounded * _TICK).quantize(Decimal("0.01"))


def marketable_limit(
    last_price: Decimal, *, side: OrderSide, buffer_pct: Decimal
) -> Decimal:
    """A limit price that crosses the spread in the direction of the trade."""
    if last_price <= 0:
        raise ValueError("last_price must be positive")
    buffer_pct = min(abs(buffer_pct), MAX_LIMIT_BUFFER_PCT)
    factor = (
        Decimal(1) + buffer_pct if side == OrderSide.BUY else Decimal(1) - buffer_pct
    )
    return _round_to_tick(last_price * factor, side=side)


def resolve_product(signal, params: dict, broker: str) -> ProductType:
    """Which product this order should use."""
    if signal.product is not None:
        return signal.product
    configured = params.get("product")
    if configured:
        return ProductType(str(configured).upper())
    return _DEFAULT_PRODUCT.get(broker, ProductType.MIS)


def build_order_request(
    *,
    signal,
    side: OrderSide,
    exchange: Exchange,
    broker: str,
    params: dict,
    last_price: Decimal | None,
) -> OrderRequest:
    """The order that expresses this signal at this broker.

    `last_price` is required only when a market order has to become a limit;
    passing None in that case is refused rather than guessed at, since a limit
    order needs a price and inventing one would be the very substitution this
    module exists to make explicit.
    """
    product = resolve_product(signal, params, broker)
    requested = signal.order_type or OrderType(
        str(params.get("order_type", OrderType.MARKET.value)).upper()
    )

    order_type = requested
    price = signal.limit_price
    trigger_price = signal.trigger_price

    needs_limit_price = requested in (OrderType.LIMIT, OrderType.SL) and price is None
    converting_market = requested == OrderType.MARKET and broker in _NO_MARKET_ORDER

    if converting_market or needs_limit_price:
        if last_price is None or last_price <= 0:
            raise FeatureNotSupportedError(
                f"{broker} needs a limit price for this order and no live price "
                "is available. Refusing rather than inventing one: an order "
                "priced off nothing is not the order the strategy asked for."
            )
        buffer_pct = signal.limit_buffer_pct
        if buffer_pct is None:
            configured = params.get("limit_buffer_pct")
            buffer_pct = (
                Decimal(str(configured))
                if configured is not None
                else DEFAULT_LIMIT_BUFFER_PCT
            )
        if abs(buffer_pct) > MAX_LIMIT_BUFFER_PCT:
            # Clamped rather than refused: the strategy's direction is still
            # coherent, only its magnitude is implausible. Logged loudly
            # because it is usually a units error (0.3 meaning 0.3% not 30%).
            logger.warning(
                "limit_buffer_clamped",
                requested=str(buffer_pct),
                allowed=str(MAX_LIMIT_BUFFER_PCT),
                symbol=signal.symbol,
            )
        price = marketable_limit(last_price, side=side, buffer_pct=buffer_pct)
        if converting_market:
            order_type = OrderType.LIMIT
            logger.info(
                "market_order_expressed_as_limit",
                broker=broker,
                symbol=signal.symbol,
                last_price=str(last_price),
                limit_price=str(price),
                side=side.value,
            )

    if order_type == OrderType.SL and trigger_price is None:
        raise FeatureNotSupportedError(
            "A stop-loss order needs a trigger price; the signal gave none."
        )

    validity = Validity(str(params.get("validity", Validity.DAY.value)).upper())

    return OrderRequest(
        symbol=signal.symbol,
        exchange=exchange,
        side=side,
        order_type=order_type,
        product=product,
        quantity=signal.quantity,
        price=price,
        trigger_price=trigger_price,
        validity=validity,
    )
