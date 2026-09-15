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


class IciciPrimePlan(BrokeragePlan):
    """ICICI Direct Prime — percentage of turnover, no cap and no floor.

    Structurally unlike a discount broker: Zerodha charges nothing on delivery
    and caps intraday at Rs 20, while this is a straight percentage that scales
    with the order. On a Rs 1 lakh delivery buy that is Rs 220 rather than Rs 0,
    and on a large order there is nothing to stop it growing.

    Rates verified from icicidirect.com/brokerage/prime-plan on 2026-09-15:
    Prime 999 0.22% delivery / 0.022% intraday; Prime 4999 0.10% / 0.010%;
    Prime 9999 0.07% / 0.007%. The same page states there is no minimum
    brokerage on equity delivery, contradicting the "or Rs 25, whichever is
    higher" floor that circulates on aggregator sites.

    The subscriptions are one-time rather than annual, so they are a sunk cost
    outside this per-trade calculation.
    """

    TIERS = {
        999: (Decimal("0.0022"), Decimal("0.00022")),
        4999: (Decimal("0.0010"), Decimal("0.00010")),
        9999: (Decimal("0.0007"), Decimal("0.00007")),
    }

    def __init__(self, tier: int = 999):
        if tier not in self.TIERS:
            raise ValueError(f"Unknown ICICI Prime tier {tier}; expected {sorted(self.TIERS)}")
        self.tier = tier
        self.delivery_rate, self.intraday_rate = self.TIERS[tier]
        self.name = f"icici_prime_{tier}"

    def charge(self, product: ProductType, turnover: Decimal) -> Decimal:
        rate = self.delivery_rate if product == ProductType.CNC else self.intraday_rate
        return turnover * rate


class IciciMoneySaverPlan(IciciPrimePlan):
    """ICICI Direct's default plan, applied when no other has been chosen.

    0.29% delivery, 0.029% intraday — the most expensive of their retail
    options, and what an account sits on if nobody opted into anything.
    """

    def __init__(self):
        self.tier = 0
        self.delivery_rate = Decimal("0.0029")
        self.intraday_rate = Decimal("0.00029")
        self.name = "icici_moneysaver"


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
    # This instance's ICICI account is on Prime 999. An account on a different
    # plan pays a different rate — MoneySaver is nearly a third more on
    # delivery — so this is per-instance configuration, not a fact about the
    # broker.
    Broker.ICICI_BREEZE: IciciPrimePlan(999),
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
