"""Indian equity charge calculation.

Paper P&L is only worth reading if it charges what a real trade would. On a
Rs 1 lakh intraday round trip the statutory stack alone is roughly Rs 60-100,
and delivery adds a flat DP charge per scrip sold — enough to turn a
marginally profitable strategy into a losing one.

Rates live in rates.py with their sources; brokerage plans in brokerage.py.
This module only assembles them.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.domain.enums import Broker, Exchange, OrderSide, ProductType
from app.engines.paper import rates
from app.engines.paper.brokerage import get_plan, is_placeholder

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
    rates_label: str = ""
    rates_effective_from: str = rates.RATES_EFFECTIVE_FROM

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
            "rates_effective_from": self.rates_effective_from,
            "rates_label": self.rates_label,
            "notes": list(self.notes),
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
    on: date | None = None,
) -> ChargeBreakdown:
    """Charges for one executed order.

    `is_first_sell_of_scrip_today` controls the DP charge, which is levied per
    scrip per day rather than per trade — selling the same scrip twice in a day
    incurs it once. The caller knows the day's history; this function does not.

    `on` is the trade date, which selects the rates in force then. Backtests
    span years and these rates move; pricing a 2023 trade with 2026 rates
    shifts every P&L in the same direction, so a strategy looks consistently
    better or worse than it was. Defaults to current rates for live trading.
    """
    breakdown = ChargeBreakdown()
    if quantity <= 0 or price <= 0:
        return breakdown
    rateset = rates.rates_for(on)
    breakdown.notes.extend(rates.uncertainty_notes(on))
    breakdown.rates_label = rateset.label
    breakdown.rates_effective_from = rateset.effective_from.isoformat()

    turnover = Decimal(quantity) * price
    is_delivery = product == ProductType.CNC
    is_buy = side == OrderSide.BUY

    breakdown.brokerage = _round(get_plan(broker).charge(product, turnover))
    if is_placeholder(broker):
        breakdown.notes.append(
            "Brokerage is a placeholder shaped like another broker's plan, not "
            "this broker's real rates — the total understates or overstates "
            "cost consistently until the real plan is encoded"
        )

    if is_delivery:
        stt_rate = rateset.stt_delivery_buy if is_buy else rateset.stt_delivery_sell
    else:
        stt_rate = rateset.stt_intraday_buy if is_buy else rateset.stt_intraday_sell
    breakdown.stt = _round(turnover * stt_rate)

    if str(getattr(exchange, "value", exchange)) == Exchange.BSE.value:
        txn_rate = rateset.bse_txn_charge
    else:
        txn_rate = rateset.nse_txn_charge + rateset.nse_ipft_charge
    breakdown.exchange_txn = _round(turnover * txn_rate)

    breakdown.sebi = _round(turnover * rateset.sebi_turnover_fee)

    if is_buy:
        stamp_rate = (
            rateset.stamp_delivery_buy if is_delivery else rateset.stamp_intraday_buy
        )
        breakdown.stamp_duty = _round(turnover * stamp_rate)

    # GST applies to brokerage, exchange charges and the SEBI fee only. STT and
    # stamp duty are statutory levies outside the GST net, and the DP charge
    # already has GST baked into its published figure.
    gst_base = breakdown.brokerage + breakdown.exchange_txn
    if rateset.sebi_fee_attracts_gst:
        gst_base += breakdown.sebi
    breakdown.gst = _round(gst_base * rateset.gst_rate)

    # Delivery sells debit the demat account, which is what triggers the DP
    # charge. Flat per scrip per day: quantity does not matter.
    if is_delivery and not is_buy and is_first_sell_of_scrip_today:
        broker_dp = rates.DP_CHARGE_BY_BROKER.get(
            str(getattr(broker, "value", broker))
        )
        if broker_dp is not None and rateset.dp_charge_per_scrip is not None:
            # The depository participant differs by broker: ICICI is its own,
            # where Zerodha routes through CDSL. Reusing one broker's figure
            # for the other misstates every delivery sell.
            breakdown.dp_charges = broker_dp
            breakdown.notes.append(
                "DP charge applies once per scrip per day. It is debited from "
                "the ledger rather than shown on the contract note, so a note "
                "that omits it is not evidence it was not charged"
            )
        elif rateset.dp_charge_per_scrip is None:
            breakdown.notes.append(
                "DP charge not modelled before 2024-10-01 (CDSL used slab "
                "rates that could not be recovered); delivery cost is "
                "understated by roughly Rs 15 per scrip per day"
            )
        else:
            breakdown.dp_charges = rateset.dp_charge_per_scrip
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
    on: date | None = None,
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
        on=on,
    ).total
