"""The risk rules every account starts with.

The risk engine loops over the user's ENABLED rules. A user with none passes
every check -- and only the demo seeder created any, which a production deploy
never runs. So a real account registered through an invite had an empty rule
set and no limit of any kind applied to it.

These are deliberately conservative. They are a floor a person raises
knowingly, not a ceiling tuned for anyone's strategy, and an account that
trades more than these allows should say so explicitly rather than inherit it.
"""

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RiskRule
from app.domain.enums import Environment, RiskRuleType

# (rule, params, why this number)
DEFAULT_RULES: list[tuple[RiskRuleType, dict]] = [
    # The capital ceilings. Neither is implied by the per-order or per-symbol
    # limits: ten orders of 50k in ten symbols pass both while committing 5
    # lakh.
    (RiskRuleType.MAX_TOTAL_EXPOSURE, {"max_exposure": 100000}),
    (RiskRuleType.MAX_DAILY_TURNOVER, {"max_turnover": 200000}),
    # A single order that is obviously wrong -- a units error, a bad LLM
    # completion -- is stopped before the ceilings even matter.
    (RiskRuleType.MAX_ORDER_NOTIONAL, {"max_notional": 25000}),
    (RiskRuleType.MAX_POSITION_SIZE, {"max_quantity": 100}),
    (RiskRuleType.MAX_OPEN_POSITIONS, {"max_positions": 5}),
    # The stop that matters most on a bad day.
    (RiskRuleType.MAX_DAILY_LOSS, {"max_loss": 5000}),
    # A strategy looping on the same signal.
    (RiskRuleType.DUPLICATE_ORDER_COOLDOWN, {"seconds": 5}),
    (RiskRuleType.MARKET_HOURS, {}),
]


async def provision(db: AsyncSession, user_id) -> int:
    """Give a new user the default rules in both environments.

    Both, because an account can be switched between them and a rule set that
    exists in one but not the other is a limit that silently disappears.

    Returns how many rules were created.
    """
    created = 0
    for environment in (Environment.PAPER, Environment.LIVE):
        for rule_type, params in DEFAULT_RULES:
            db.add(
                RiskRule(
                    user_id=user_id,
                    environment=environment.value,
                    rule_type=rule_type.value,
                    params=dict(params),
                    enabled=True,
                )
            )
            created += 1
    return created


# Which params key each rule's limit lives under, and what it measures. The
# UI needs this to offer a single editable number rather than raw JSON -- a
# limit that gates real money should not be edited by hand-writing a dict,
# where a typo in a key name silently removes the limit rather than changing
# it.
#
# Declared here, beside describe(), so the key and its wording cannot drift.
EDITABLE_FIELD: dict[RiskRuleType, tuple[str, str]] = {
    RiskRuleType.MAX_TOTAL_EXPOSURE: ("max_exposure", "rupees"),
    RiskRuleType.MAX_DAILY_TURNOVER: ("max_turnover", "rupees"),
    RiskRuleType.MAX_ORDER_NOTIONAL: ("max_notional", "rupees"),
    RiskRuleType.MAX_DAILY_LOSS: ("max_loss", "rupees"),
    RiskRuleType.MAX_POSITION_SIZE: ("max_quantity", "shares"),
    RiskRuleType.MAX_OPEN_POSITIONS: ("max_positions", "positions"),
    RiskRuleType.DUPLICATE_ORDER_COOLDOWN: ("seconds", "seconds"),
    # MARKET_HOURS takes no parameter: it is on or off.
}


def editable_field(rule_type: RiskRuleType) -> tuple[str, str] | None:
    """The single params key a user may edit, and its unit, or None."""
    return EDITABLE_FIELD.get(rule_type)


def describe(rule_type: RiskRuleType, params: dict) -> str:
    """A one-line summary for the UI, in the units the rule actually uses."""
    if rule_type == RiskRuleType.MAX_TOTAL_EXPOSURE:
        return f"Never hold more than ₹{Decimal(str(params.get('max_exposure', 0))):,.0f} at once"
    if rule_type == RiskRuleType.MAX_DAILY_TURNOVER:
        return f"Buy at most ₹{Decimal(str(params.get('max_turnover', 0))):,.0f} per day"
    if rule_type == RiskRuleType.MAX_ORDER_NOTIONAL:
        return f"No single order over ₹{Decimal(str(params.get('max_notional', 0))):,.0f}"
    if rule_type == RiskRuleType.MAX_DAILY_LOSS:
        return f"Stop trading after ₹{Decimal(str(params.get('max_loss', 0))):,.0f} of losses"
    if rule_type == RiskRuleType.MAX_POSITION_SIZE:
        return f"At most {params.get('max_quantity', 0)} shares in one symbol"
    if rule_type == RiskRuleType.MAX_OPEN_POSITIONS:
        return f"At most {params.get('max_positions', 0)} open positions"
    if rule_type == RiskRuleType.DUPLICATE_ORDER_COOLDOWN:
        return f"Same order at most once every {params.get('seconds', 0)}s"
    if rule_type == RiskRuleType.MARKET_HOURS:
        return "Only during NSE market hours"
    return ""
