"""Risk-adjusted performance metrics for a backtest equity curve.

Total return and win rate say almost nothing on their own: a 12% return that
swung 40% along the way is a different strategy from a 12% that crept up, and
a 70% win rate hides catastrophe if the 30% are large. These are the measures
that separate them.

Formulas follow FinanceToolkit's definitions (vendored at
skills/coding/finance-tools/finance-toolkit-lib), which cite their sources and
are explicit about the conventions that are easy to get wrong. Implemented
directly on Decimal rather than by importing the library: it is built around
fetching ticker data through pandas/numpy for securities analysis, and pulling
that dependency into the API service to score an in-memory curve would be a
poor trade.

Everything is computed from the periodic returns of the equity curve, so the
same code serves daily, 5-minute or 1-minute backtests provided the caller
says how many periods make a year.
"""

from decimal import Decimal, InvalidOperation

# Trading days in an Indian equity year, after weekends and ~15 holidays.
TRADING_DAYS_PER_YEAR = 250
# NSE runs 09:15-15:30, so 375 one-minute bars or 75 five-minute bars a day.
MINUTES_PER_SESSION = 375

# India's risk-free proxy is the short-dated government yield. A rate that is
# wrong by a point moves Sharpe noticeably, so it is a parameter with a stated
# default rather than a constant buried in the maths.
DEFAULT_RISK_FREE_RATE = Decimal("0.065")

# Annualisation multiplies by sqrt(periods_per_year), so a handful of bars
# scaled to a 93,750-period year yields a Sharpe in the hundreds -- noise
# wearing a suit. Below this many observations the ratios are reported as
# undefined rather than as a number nobody should act on. Thirty is the
# conventional floor for treating a sample mean as meaningful at all.
MIN_OBSERVATIONS_FOR_RATIOS = 30


def periods_per_year(interval: str) -> int:
    """How many bars of this interval make a trading year."""
    mapping = {
        "1d": TRADING_DAYS_PER_YEAR,
        "1m": TRADING_DAYS_PER_YEAR * MINUTES_PER_SESSION,
        "5m": TRADING_DAYS_PER_YEAR * (MINUTES_PER_SESSION // 5),
        "15m": TRADING_DAYS_PER_YEAR * (MINUTES_PER_SESSION // 15),
        "60m": TRADING_DAYS_PER_YEAR * 6,
    }
    if interval not in mapping:
        raise ValueError(f"Unknown interval {interval!r}; expected one of {sorted(mapping)}")
    return mapping[interval]


def _sqrt(value: Decimal) -> Decimal:
    if value <= 0:
        return Decimal(0)
    try:
        return value.sqrt()
    except InvalidOperation:
        return Decimal(0)


def returns_from_equity(curve: list[Decimal]) -> list[Decimal]:
    """Simple period-over-period returns.

    Simple rather than log returns: they aggregate across a portfolio the way
    money actually does, and the ratios below are conventionally quoted on
    them. A zero or negative equity point ends the series — the account is
    wiped out, and later "returns" would be meaningless.
    """
    out = []
    for previous, current in zip(curve, curve[1:]):
        if previous <= 0:
            break
        out.append((current - previous) / previous)
    return out


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values) / Decimal(len(values)) if values else Decimal(0)


def _stdev(values: list[Decimal]) -> Decimal:
    """Sample standard deviation (n-1), matching the convention used when
    returns are treated as a sample of a strategy's behaviour."""
    if len(values) < 2:
        return Decimal(0)
    mu = _mean(values)
    variance = sum((v - mu) ** 2 for v in values) / Decimal(len(values) - 1)
    return _sqrt(variance)


def _downside_deviation(excess: list[Decimal]) -> Decimal:
    """Root-mean-square of the returns below zero, over the TOTAL count.

    The divisor is N, not the number of negative observations. Dividing by the
    count of losses instead would measure how spread out the losses are around
    their own mean, which understates downside risk exactly when it matters:
    a strategy with few but enormous losses would look calm.
    """
    if not excess:
        return Decimal(0)
    squares = sum(min(r, Decimal(0)) ** 2 for r in excess)
    return _sqrt(squares / Decimal(len(excess)))


def sharpe_ratio(
    returns: list[Decimal],
    *,
    periods: int,
    risk_free_rate: Decimal = DEFAULT_RISK_FREE_RATE,
) -> Decimal | None:
    """Annualised mean excess return per unit of total volatility.

    Returns None rather than a number when volatility is zero: a ratio with a
    zero denominator is undefined, and reporting 0 or infinity would both be
    lies about a curve that never moved.
    """
    if len(returns) < MIN_OBSERVATIONS_FOR_RATIOS:
        return None
    rf_per_period = risk_free_rate / Decimal(periods)
    excess = [r - rf_per_period for r in returns]
    sigma = _stdev(excess)
    if sigma == 0:
        return None
    return (_mean(excess) / sigma) * _sqrt(Decimal(periods))


def sortino_ratio(
    returns: list[Decimal],
    *,
    periods: int,
    risk_free_rate: Decimal = DEFAULT_RISK_FREE_RATE,
) -> Decimal | None:
    """Annualised mean excess return per unit of DOWNSIDE deviation.

    Sharpe penalises upside volatility as if it were risk; Sortino does not,
    which is the more honest question for a strategy whose good days are large.
    """
    if len(returns) < MIN_OBSERVATIONS_FOR_RATIOS:
        return None
    rf_per_period = risk_free_rate / Decimal(periods)
    excess = [r - rf_per_period for r in returns]
    downside = _downside_deviation(excess)
    if downside == 0:
        return None
    return (_mean(excess) / downside) * _sqrt(Decimal(periods))


def calmar_ratio(annualised_return: Decimal, max_drawdown: Decimal) -> Decimal | None:
    """Annualised return per unit of worst drawdown.

    The question an investor actually asks: what did I earn for the worst loss
    I had to sit through.
    """
    if max_drawdown <= 0:
        return None
    return annualised_return / abs(max_drawdown)


def annualised_return(
    total_return: Decimal, *, observations: int, periods: int
) -> Decimal | None:
    """Compound the realised return out to a year.

    Undefined for a total loss (the curve hit zero) and not reported for very
    short samples, where annualising a few bars produces a confident-looking
    number with no meaning behind it.
    """
    if observations < MIN_OBSERVATIONS_FOR_RATIOS or periods <= 0:
        return None
    growth = Decimal(1) + total_return
    if growth <= 0:
        return None
    exponent = Decimal(periods) / Decimal(observations)
    try:
        return (growth ** exponent) - Decimal(1)
    except (InvalidOperation, OverflowError):
        return None


def value_at_risk(returns: list[Decimal], *, confidence: Decimal = Decimal("0.95")) -> Decimal | None:
    """Historical VaR: the loss that is exceeded (1 - confidence) of the time.

    Historical rather than parametric — no assumption that returns are normal,
    which they are not, least of all in the tails that VaR is about.
    Returned as a negative number, the sign of the loss.
    """
    if len(returns) < 2:
        return None
    ordered = sorted(returns)
    # The (1-confidence) quantile, counting from the worst. With 100 returns
    # at 95%, that is the 5th worst -- index 4, not 5: index 5 is the sixth
    # worst and would step past the tail entirely when there are exactly five
    # losses, reporting a gain as the value at risk.
    rank = int((Decimal(1) - confidence) * Decimal(len(ordered)))
    index = max(0, min(rank - 1, len(ordered) - 1))
    return ordered[index]


def conditional_value_at_risk(
    returns: list[Decimal], *, confidence: Decimal = Decimal("0.95")
) -> Decimal | None:
    """Expected loss GIVEN that the VaR threshold was breached.

    VaR says how bad a bad day is; CVaR says how bad the bad days are on
    average once you are in one. For a strategy that can blow up, this is the
    more informative of the two.
    """
    if len(returns) < 2:
        return None
    ordered = sorted(returns)
    cutoff = max(1, int((Decimal(1) - confidence) * Decimal(len(ordered))))
    tail = ordered[:cutoff]
    return _mean(tail) if tail else None


def profit_factor(wins: list[Decimal], losses: list[Decimal]) -> Decimal | None:
    """Gross profit divided by gross loss.

    Below 1 the strategy loses money regardless of its win rate, which is what
    makes this the check on a high-win-rate strategy that risks a lot to make
    a little.
    """
    gross_loss = abs(sum(losses))
    if gross_loss == 0:
        return None
    return sum(wins) / gross_loss


def summarise(
    *,
    equity_curve: list[Decimal],
    interval: str,
    trade_pnls: list[Decimal],
    total_return: Decimal,
    max_drawdown: Decimal,
    exposure_bars: int,
    risk_free_rate: Decimal = DEFAULT_RISK_FREE_RATE,
) -> dict:
    """Every risk metric for one backtest, as JSON-safe strings.

    A metric that cannot be computed is None rather than zero: "undefined"
    and "zero" mean very different things about a strategy, and collapsing
    them would flatter a curve that never traded.
    """
    periods = periods_per_year(interval)
    returns = returns_from_equity(equity_curve)
    observations = len(returns)

    annual = annualised_return(
        total_return, observations=observations, periods=periods
    )
    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]

    def out(value):
        return str(value) if value is not None else None

    return {
        "annualised_return": out(annual),
        "sharpe_ratio": out(
            sharpe_ratio(returns, periods=periods, risk_free_rate=risk_free_rate)
        ),
        "sortino_ratio": out(
            sortino_ratio(returns, periods=periods, risk_free_rate=risk_free_rate)
        ),
        "calmar_ratio": out(
            calmar_ratio(annual, max_drawdown) if annual is not None else None
        ),
        "value_at_risk_95": out(value_at_risk(returns)),
        "conditional_value_at_risk_95": out(conditional_value_at_risk(returns)),
        "profit_factor": out(profit_factor(wins, losses)),
        "average_win": out(_mean(wins) if wins else None),
        "average_loss": out(_mean(losses) if losses else None),
        "largest_win": out(max(wins) if wins else None),
        "largest_loss": out(min(losses) if losses else None),
        # Time in the market. A return earned while invested a fifth of the
        # time is a different proposition from the same return fully invested.
        "exposure": out(
            Decimal(exposure_bars) / Decimal(len(equity_curve))
            if equity_curve
            else None
        ),
        "observations": observations,
        "risk_free_rate": str(risk_free_rate),
        "periods_per_year": periods,
    }
