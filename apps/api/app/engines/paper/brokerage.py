"""Per-broker brokerage plans.

Everything else in the charge stack is statutory or exchange-level and
identical across brokers; brokerage is the one component a broker sets. Each
broker supplies a rule, so adding Groww or Breeze later means adding a plan
here rather than touching the calculation.
"""

from decimal import Decimal

from app.domain.enums import Broker, ProductType


class BrokeragePlan:
    """A broker's equity brokerage rule."""

    name = "unknown"

    def charge(self, product: ProductType, turnover: Decimal) -> Decimal:
        raise NotImplementedError


class ZerodhaPlan(BrokeragePlan):
    """Zerodha equity: free delivery, capped percentage intraday.

    Intraday is 0.03% or Rs 20 per executed order, whichever is LOWER — so a
    small order pays the percentage and a large one pays the cap. Charged per
    executed order, not per fill: an order that fills in five trades is billed
    once. Buy and sell are separate orders, so an intraday round trip can pay
    the cap twice.
    """

    name = "zerodha"
    INTRADAY_RATE = Decimal("0.0003")
    INTRADAY_CAP = Decimal("20")

    def charge(self, product: ProductType, turnover: Decimal) -> Decimal:
        if product == ProductType.CNC:
            return Decimal("0")
        return min(turnover * self.INTRADAY_RATE, self.INTRADAY_CAP)


class ZeroBrokeragePlan(BrokeragePlan):
    """Used by the paper simulator's own account, where there is no broker
    taking a cut. Statutory charges still apply — those are what make paper
    P&L honest."""

    name = "paper"

    def charge(self, product: ProductType, turnover: Decimal) -> Decimal:
        return Decimal("0")


# Groww and Breeze keep Zerodha-shaped pricing until their adapters are
# verified and their real plans confirmed. Both advertise similar discount
# structures, but an unverified number here would quietly misstate cost, so
# this is a placeholder to replace in their adapter phases, not a claim about
# what they charge.
_PLANS: dict[Broker, BrokeragePlan] = {
    Broker.PAPER: ZeroBrokeragePlan(),
    Broker.ZERODHA: ZerodhaPlan(),
    Broker.GROWW: ZerodhaPlan(),
    Broker.ICICI_BREEZE: ZerodhaPlan(),
}


def get_plan(broker: Broker | str) -> BrokeragePlan:
    if isinstance(broker, str):
        broker = Broker(broker)
    return _PLANS[broker]
