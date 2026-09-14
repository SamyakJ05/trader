"""Risk-adjusted performance metrics.

Formulas follow FinanceToolkit's definitions. Several of these tests exist
because the naive implementation of the same metric is subtly different and
flatters the strategy — those are called out individually.
"""

from decimal import Decimal

import pytest

from app.engines import metrics as m


def D(x):
    return Decimal(str(x))


def curve(*values):
    return [D(v) for v in values]


def repeat_curve(pattern, cycles=20, start=D(100)):
    """A curve long enough to clear MIN_OBSERVATIONS_FOR_RATIOS.

    The annualised ratios refuse short samples, so any test of their VALUE
    (rather than of the refusal itself) needs a realistic number of bars.
    `pattern` is a list of per-period multipliers applied in sequence.
    """
    out, equity = [start], start
    for _ in range(cycles):
        for factor in pattern:
            equity = equity * D(factor)
            out.append(equity)
    return out


# ── returns from an equity curve ─────────────────────────────────────


def test_returns_are_period_over_period():
    assert m.returns_from_equity(curve(100, 110, 99)) == [D("0.1"), D("-0.1")]


def test_a_flat_curve_has_zero_returns():
    assert m.returns_from_equity(curve(100, 100, 100)) == [D(0), D(0)]


def test_a_single_point_has_no_returns():
    assert m.returns_from_equity(curve(100)) == []


def test_the_series_stops_at_a_wipeout():
    """Once equity hits zero the account is gone; later percentage changes
    would be arithmetic on a corpse."""
    assert m.returns_from_equity(curve(100, 0, 50)) == [D(-1)]


# ── Sharpe ───────────────────────────────────────────────────────────


def test_sharpe_is_positive_for_a_steadily_rising_curve():
    returns = m.returns_from_equity(repeat_curve(["1.01", "1.005"]))
    assert m.sharpe_ratio(returns, periods=250) > 0


def test_sharpe_is_negative_for_a_falling_curve():
    returns = m.returns_from_equity(repeat_curve(["0.99", "0.995"]))
    assert m.sharpe_ratio(returns, periods=250) < 0


def test_sharpe_is_undefined_when_there_is_no_volatility():
    """A ratio over zero volatility is undefined. Reporting 0 would read as
    'mediocre' and infinity as 'perfect'; both misdescribe a flat line."""
    assert m.sharpe_ratio([D(0), D(0), D(0)], periods=250) is None


def test_sharpe_needs_at_least_two_observations():
    assert m.sharpe_ratio([D("0.01")], periods=250) is None


def test_sharpe_falls_when_the_same_return_is_more_volatile():
    """The whole point of risk adjustment: same destination, rougher ride,
    lower score."""
    smooth = m.returns_from_equity(repeat_curve(["1.01", "1.01"]))
    choppy = m.returns_from_equity(repeat_curve(["1.20", "0.8417"]))
    assert m.sharpe_ratio(smooth, periods=250) > m.sharpe_ratio(choppy, periods=250)


def test_a_higher_risk_free_rate_lowers_sharpe():
    returns = m.returns_from_equity(repeat_curve(["1.01", "1.002"]))
    low = m.sharpe_ratio(returns, periods=250, risk_free_rate=D("0.00"))
    high = m.sharpe_ratio(returns, periods=250, risk_free_rate=D("0.20"))
    assert low > high


# ── Sortino ──────────────────────────────────────────────────────────


def test_sortino_exceeds_sharpe_when_volatility_is_mostly_upside():
    """Sharpe punishes large gains as 'risk'; Sortino does not. A strategy
    whose swings are upward should score better on Sortino."""
    returns = m.returns_from_equity(repeat_curve(["1.30", "0.985"]))
    sharpe = m.sharpe_ratio(returns, periods=250)
    sortino = m.sortino_ratio(returns, periods=250)
    assert sortino > sharpe


def test_sortino_is_undefined_without_downside():
    """No return below the risk-free rate means no downside deviation."""
    returns = [D("0.05")] * 5
    assert m.sortino_ratio(returns, periods=250) is None


def test_downside_deviation_divides_by_total_observations():
    """The trap FinanceToolkit documents: dividing by the COUNT OF LOSSES
    instead of the total measures how spread out the losses are around their
    own mean, which understates risk precisely when losses are few and huge.

    One -10% in ten periods: rms over N=10 is sqrt(0.01/10) = 0.0316.
    Dividing by the single loss instead would give 0.10 — or, using a sample
    stdev of one observation, zero.
    """
    excess = [D("0.01")] * 9 + [D("-0.10")]
    assert m._downside_deviation(excess).quantize(D("0.0001")) == D("0.0316")


def test_downside_deviation_counts_only_losses_in_the_numerator():
    """Gains contribute nothing to the sum but still count towards N."""
    only_gains = [D("0.05")] * 4
    assert m._downside_deviation(only_gains) == 0


def test_ratios_refuse_a_sample_too_short_to_annualise():
    """Annualising nine minute-bars to a 93,750-period year produced a Sharpe
    of -288 in practice: noise scaled by sqrt(periods). Undefined is the
    honest answer."""
    short = m.returns_from_equity(curve(100, 101, 102, 103, 104))
    assert len(short) < m.MIN_OBSERVATIONS_FOR_RATIOS
    assert m.sharpe_ratio(short, periods=250) is None
    assert m.sortino_ratio(short, periods=250) is None
    assert m.annualised_return(D("0.1"), observations=len(short), periods=250) is None


# ── Calmar ───────────────────────────────────────────────────────────


def test_calmar_is_return_over_worst_drawdown():
    assert m.calmar_ratio(D("0.30"), D("0.15")) == D(2)


def test_calmar_is_undefined_without_a_drawdown():
    assert m.calmar_ratio(D("0.30"), D(0)) is None


# ── annualisation ────────────────────────────────────────────────────


def test_annualising_a_full_year_returns_the_same_figure():
    result = m.annualised_return(D("0.20"), observations=250, periods=250)
    assert result.quantize(D("0.0001")) == D("0.2000")


def test_annualising_a_half_year_compounds_up():
    """10% in half a year compounds to 21%, not 20%."""
    result = m.annualised_return(D("0.10"), observations=125, periods=250)
    assert result.quantize(D("0.01")) == D("0.21")


def test_annualising_a_total_loss_is_undefined():
    assert m.annualised_return(D("-1"), observations=250, periods=250) is None


def test_annualising_needs_more_than_one_observation():
    assert m.annualised_return(D("0.1"), observations=1, periods=250) is None


# ── VaR and CVaR ─────────────────────────────────────────────────────


def test_var_is_a_loss_in_the_left_tail():
    returns = [D("0.05")] * 95 + [D("-0.10")] * 5
    assert m.value_at_risk(returns) < 0


def test_cvar_is_at_least_as_severe_as_var():
    """CVaR averages the tail beyond VaR, so it can never be the milder
    number."""
    returns = [D("0.01")] * 90 + [D("-0.05")] * 5 + [D("-0.50")] * 5
    var = m.value_at_risk(returns)
    cvar = m.conditional_value_at_risk(returns)
    assert cvar <= var


def test_cvar_sees_a_fat_tail_that_var_misses():
    """Two curves with the same VaR: the one whose worst losses are far worse
    must report a worse CVaR. This is why both are reported."""
    mild = [D("0.01")] * 95 + [D("-0.05")] * 5
    severe = [D("0.01")] * 95 + [D("-0.05")] * 4 + [D("-0.80")]
    assert m.conditional_value_at_risk(severe) < m.conditional_value_at_risk(mild)


# ── profit factor ────────────────────────────────────────────────────


def test_profit_factor_above_one_means_gross_profit_exceeds_gross_loss():
    assert m.profit_factor([D(300), D(200)], [D(-100)]) == D(5)


def test_profit_factor_below_one_exposes_a_high_win_rate_loser():
    """Nine wins of 10 and one loss of 200: a 90% win rate that loses money.
    Win rate alone would call this excellent."""
    wins = [D(10)] * 9
    losses = [D(-200)]
    assert m.profit_factor(wins, losses) < 1


def test_profit_factor_is_undefined_without_losses():
    assert m.profit_factor([D(100)], []) is None


# ── the summary ──────────────────────────────────────────────────────


def test_summary_reports_every_metric_as_strings_or_none():
    summary = m.summarise(
        equity_curve=curve(100, 105, 103, 110, 108, 115),
        interval="1d",
        trade_pnls=[D(50), D(-20), D(30)],
        total_return=D("0.15"),
        max_drawdown=D("0.02"),
        exposure_bars=4,
    )
    for key in ("sharpe_ratio", "sortino_ratio", "profit_factor", "exposure"):
        assert summary[key] is None or isinstance(summary[key], str)
    assert summary["periods_per_year"] == 250
    assert summary["observations"] == 5


def test_summary_distinguishes_undefined_from_zero():
    """A curve that never moved has no Sharpe. Reporting "0" would imply a
    measured, mediocre result rather than an unmeasurable one."""
    summary = m.summarise(
        equity_curve=curve(100, 100, 100),
        interval="1d",
        trade_pnls=[],
        total_return=D(0),
        max_drawdown=D(0),
        exposure_bars=0,
    )
    assert summary["sharpe_ratio"] is None
    assert summary["profit_factor"] is None
    assert summary["calmar_ratio"] is None


def test_exposure_is_the_fraction_of_bars_holding_a_position():
    summary = m.summarise(
        equity_curve=curve(100, 101, 102, 103),
        interval="1d",
        trade_pnls=[],
        total_return=D("0.03"),
        max_drawdown=D(0),
        exposure_bars=2,
    )
    assert Decimal(summary["exposure"]) == D("0.5")


# ── interval handling ────────────────────────────────────────────────


def test_intervals_map_to_sensible_annual_period_counts():
    assert m.periods_per_year("1d") == 250
    assert m.periods_per_year("5m") == 250 * 75
    assert m.periods_per_year("1m") == 250 * 375


def test_an_unknown_interval_is_refused():
    """Silently defaulting would annualise minute bars as if they were days,
    inflating Sharpe by roughly twenty times."""
    with pytest.raises(ValueError):
        m.periods_per_year("3h")


def test_the_same_curve_annualises_differently_by_interval():
    returns = m.returns_from_equity(repeat_curve(["1.01", "1.002"]))
    daily = m.sharpe_ratio(returns, periods=m.periods_per_year("1d"))
    minute = m.sharpe_ratio(returns, periods=m.periods_per_year("1m"))
    assert minute > daily
