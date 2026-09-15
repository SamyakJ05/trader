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
    is_placeholder = False

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


class PlaceholderPlan(ZerodhaPlan):
    """Zerodha-shaped pricing standing in for a broker whose real plan is not
    yet encoded.

    Named rather than silent because the difference matters: ICICI Direct is a
    full-service broker whose percentage-based rates with a per-order floor are
    nothing like a discount broker's flat cap. Every P&L and backtest on such
    an account is wrong in the same direction until its real plan lands, and a
    consistent bias is exactly the kind of error that survives a glance at the
    numbers.
    """

    name = "placeholder"
    is_placeholder = True


# Groww and Breeze carry placeholder pricing until their real plans are
# confirmed. Both advertise structures unlike Zerodha's, so these are markers
# to replace, not claims about what they charge.
_PLANS: dict[Broker, BrokeragePlan] = {
    Broker.PAPER: ZeroBrokeragePlan(),
    Broker.ZERODHA: ZerodhaPlan(),
    Broker.GROWW: PlaceholderPlan(),
    Broker.ICICI_BREEZE: PlaceholderPlan(),
}


def is_placeholder(broker: Broker | str) -> bool:
    """Whether this broker's brokerage is a stand-in rather than its real plan.

    Callers surface this so a cost figure is not read as authoritative when it
    is a guess shaped like another broker's pricing.
    """
    return getattr(get_plan(broker), "is_placeholder", False)


def get_plan(broker: Broker | str) -> BrokeragePlan:
    if isinstance(broker, str):
        broker = Broker(broker)
    return _PLANS[broker]
