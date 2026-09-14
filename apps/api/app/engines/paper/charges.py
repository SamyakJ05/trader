"""Indian equity charge calculation.

Paper P&L is only worth reading if it charges what a real trade would. On a
Rs 1 lakh intraday round trip the statutory stack alone is roughly Rs 60-100,
and delivery adds a flat DP charge per scrip sold — enough to turn a
marginally profitable strategy into a losing one.

Rates live in rates.py with their sources; brokerage plans in brokerage.py.
This module only assembles them.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from app.domain.enums import Broker, Exchange, OrderSide, ProductType
from app.engines.paper import rates
from app.engines.paper.brokerage import get_plan

PAISE = Decimal("0.01")


def _round(value: Decimal) -> Decimal:
    """Round to paise, half-up, the way money is quoted.

    Decimal's default is banker's rounding, which would systematically differ
    from a contract note on half-paise values.
    """
    return value.quantize(PAISE, rounding="ROUND_HALF_UP")


@dataclass
class ChargeBreakdown:
    """Every component, so the UI can show where the money went rather than a
    single unexplained number."""

    brokerage: Decimal = Decimal("0")
    stt: Decimal = Decimal("0")
    exchange_txn: Decimal = Decimal("0")
    sebi: Decimal = Decimal("0")
    stamp_duty: Decimal = Decimal("0")
    gst: Decimal = Decimal("0")
    dp_charges: Decimal = Decimal("0")
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return _round(
            self.brokerage
            + self.stt
            + self.exchange_txn
            + self.sebi
            + self.stamp_duty
            + self.gst
            + self.dp_charges
        )

    def as_dict(self) -> dict:
        return {
            "brokerage": str(self.brokerage),
            "stt": str(self.stt),
            "exchange_txn": str(self.exchange_txn),
            "sebi": str(self.sebi),
            "stamp_duty": str(self.stamp_duty),
            "gst": str(self.gst),
            "dp_charges": str(self.dp_charges),
            "total": str(self.total),
            "rates_effective_from": rates.RATES_EFFECTIVE_FROM,
        }


def compute_charges(
    *,
    broker: Broker | str,
    side: OrderSide,
    product: ProductType,
    quantity: int,
    price: Decimal,
    exchange: Exchange | str = Exchange.NSE,
    is_first_sell_of_scrip_today: bool = True,
) -> ChargeBreakdown:
    """Charges for one executed order.

    `is_first_sell_of_scrip_today` controls the DP charge, which is levied per
    scrip per day rather than per trade — selling the same scrip twice in a day
    incurs it once. The caller knows the day's history; this function does not.
    """
    breakdown = ChargeBreakdown()
    if quantity <= 0 or price <= 0:
        return breakdown

    turnover = Decimal(quantity) * price
    is_delivery = product == ProductType.CNC
    is_buy = side == OrderSide.BUY

    breakdown.brokerage = _round(get_plan(broker).charge(product, turnover))

    if is_delivery:
        stt_rate = rates.STT_DELIVERY_BUY if is_buy else rates.STT_DELIVERY_SELL
    else:
        stt_rate = rates.STT_INTRADAY_BUY if is_buy else rates.STT_INTRADAY_SELL
    breakdown.stt = _round(turnover * stt_rate)

    if str(getattr(exchange, "value", exchange)) == Exchange.BSE.value:
        txn_rate = rates.BSE_TXN_CHARGE
    else:
        txn_rate = rates.NSE_TXN_CHARGE + rates.NSE_IPFT_CHARGE
    breakdown.exchange_txn = _round(turnover * txn_rate)

    breakdown.sebi = _round(turnover * rates.SEBI_TURNOVER_FEE)

    if is_buy:
        stamp_rate = (
            rates.STAMP_DELIVERY_BUY if is_delivery else rates.STAMP_INTRADAY_BUY
        )
        breakdown.stamp_duty = _round(turnover * stamp_rate)

    # GST applies to brokerage, exchange charges and the SEBI fee only. STT and
    # stamp duty are statutory levies outside the GST net, and the DP charge
    # already has GST baked into its published figure.
    breakdown.gst = _round(
        (breakdown.brokerage + breakdown.exchange_txn + breakdown.sebi) * rates.GST_RATE
    )

    # Delivery sells debit the demat account, which is what triggers the DP
    # charge. Flat per scrip per day: quantity does not matter.
    if is_delivery and not is_buy and is_first_sell_of_scrip_today:
        breakdown.dp_charges = rates.DP_CHARGE_PER_SCRIP
        breakdown.notes.append("DP charge applies once per scrip per day")

    return breakdown


def estimate_charges(
    side: OrderSide,
    product: ProductType,
    quantity: int,
    price: Decimal,
    *,
    broker: Broker | str = Broker.PAPER,
    exchange: Exchange | str = Exchange.NSE,
    is_first_sell_of_scrip_today: bool = True,
) -> Decimal:
    """Total charges. Kept as the existing call shape so the fill pipeline
    does not change; use compute_charges when the breakdown is wanted."""
    return compute_charges(
        broker=broker,
        side=side,
        product=product,
        quantity=quantity,
        price=price,
        exchange=exchange,
        is_first_sell_of_scrip_today=is_first_sell_of_scrip_today,
    ).total
