"""How a signal becomes an order at a particular broker.

The runner used to hardcode MARKET/MIS. Breeze accepts neither -- its API
takes only limit and stoploss, and it has no MIS equivalent -- so every signal
a Breeze strategy produced passed the risk engine, was recorded as an order,
and then failed at the adapter. These pin the conversion and, more
importantly, its limits: the point where a strategy's stated intent stops
being obeyed.
"""

from decimal import Decimal

import pytest

from app.adapters.base import FeatureNotSupportedError
from app.domain.enums import (
    Broker,
    Exchange,
    OptionRight,
    OrderSide,
    OrderType,
    ProductType,
    SignalType,
)
from app.engines.strategy import execution
from app.engines.strategy.base import Signal


def signal(**kw):
    base = dict(symbol="RELIANCE", signal_type=SignalType.ENTRY_LONG, quantity=10)
    base.update(kw)
    return Signal(**base)


def build(sig, *, broker=Broker.ICICI_BREEZE.value, params=None, last=Decimal("2800"),
          side=OrderSide.BUY, exchange=None):
    # Exchange follows the params unless overridden: a test that configures an
    # expiry is describing a derivatives order, and asserting an NFO default
    # while passing NSE would test nothing.
    if exchange is None:
        exchange = Exchange.NFO if (params or {}).get("expiry") else Exchange.NSE
    return execution.build_order_request(
        signal=sig, side=side, exchange=exchange, broker=broker,
        params=params or {}, last_price=last,
    )


# ── the conversion Breeze requires ───────────────────────────────────


def test_a_market_signal_becomes_a_marketable_limit_for_breeze():
    """Breeze's API has no market order at all. Refusing outright would make
    every simple strategy unusable on it, so "buy now" is expressed the way
    this broker spells it: a limit placed through the last trade so it
    crosses the spread rather than resting."""
    request = build(signal())
    assert request.order_type is OrderType.LIMIT
    assert request.price > Decimal("2800")


def test_a_sell_limit_sits_below_the_last_price():
    """The buffer must follow the direction of the trade. Adding it to a sell
    would place the order away from the book, where it never fills."""
    request = build(signal(signal_type=SignalType.EXIT_LONG), side=OrderSide.SELL)
    assert request.price < Decimal("2800")


def test_zerodha_keeps_its_market_order():
    """Kite accepts market orders. Converting there would substitute an order
    nobody asked for, and the conversion exists only because Breeze cannot
    express the original."""
    request = build(signal(), broker=Broker.ZERODHA.value)
    assert request.order_type is OrderType.MARKET
    assert request.price is None


# ── the strategy decides, not the platform ───────────────────────────


def test_a_strategy_sets_its_own_aggression_per_signal():
    """A momentum entry that must not be missed and a patient exit want
    different buffers. One platform-wide constant would serve one badly to
    serve the other."""
    patient = build(signal(limit_buffer_pct=Decimal("0.001")))
    urgent = build(signal(limit_buffer_pct=Decimal("0.02")))
    assert patient.price < urgent.price


def test_a_strategy_can_name_an_exact_limit_price():
    """A strategy that has computed its own price should get that price, not
    a price derived from the last tick."""
    request = build(signal(order_type=OrderType.LIMIT, limit_price=Decimal("2750")))
    assert request.order_type is OrderType.LIMIT
    assert request.price == Decimal("2750")


def test_params_set_the_default_for_a_strategy_that_says_nothing_per_signal():
    request = build(signal(), params={"limit_buffer_pct": "0.01"})
    # 2800 * 1.01 = 2828
    assert request.price >= Decimal("2828")


def test_the_signal_overrides_the_strategys_own_params():
    """Precedence is most-specific-first: this trade beats every trade."""
    request = build(
        signal(limit_buffer_pct=Decimal("0.001")), params={"limit_buffer_pct": "0.03"}
    )
    assert request.price < Decimal("2810")


# ── the floor: where intent stops being obeyed ───────────────────────


def test_an_implausible_buffer_is_clamped_not_executed():
    """A strategy asking to pay 50% through the last trade has a bug -- a
    units error, or a bad LLM completion. Executing a bug is worse than
    refusing it, so the reach is capped however aggressive the request."""
    request = build(signal(limit_buffer_pct=Decimal("0.5")))
    ceiling = Decimal("2800") * (1 + execution.MAX_LIMIT_BUFFER_PCT)
    assert request.price <= ceiling.quantize(Decimal("0.01")) + Decimal("0.05")


def test_a_limit_order_without_a_price_is_refused_when_nothing_can_price_it():
    """An order priced off nothing is not the order the strategy asked for.
    Inventing a price here is exactly the substitution this module exists to
    make explicit."""
    with pytest.raises(FeatureNotSupportedError, match="no live price"):
        build(signal(), last=None)


def test_a_stop_loss_without_a_trigger_is_refused():
    with pytest.raises(FeatureNotSupportedError, match="trigger price"):
        build(signal(order_type=OrderType.SL, limit_price=Decimal("2700")))


# ── product ──────────────────────────────────────────────────────────


def test_breeze_defaults_to_delivery_not_intraday():
    """Breeze has no MIS equivalent, so MIS -- the platform's old hardcoded
    default -- was a guaranteed rejection. CNC is the honest default: it is
    what the account already holds and is not force-squared-off at 3:20pm."""
    assert build(signal()).product is ProductType.CNC


def test_zerodha_keeps_intraday_as_its_default():
    assert build(signal(), broker=Broker.ZERODHA.value).product is ProductType.MIS


def test_a_signal_can_choose_its_product():
    request = build(signal(product=ProductType.NRML))
    assert request.product is ProductType.NRML


# ── tick rounding ────────────────────────────────────────────────────


def test_prices_land_on_a_real_tick():
    """Indian equities quote in paise. An exchange rejects a price that is not
    a multiple of the tick size."""
    price = execution.marketable_limit(
        Decimal("2800.37"), side=OrderSide.BUY, buffer_pct=Decimal("0.003")
    )
    assert (price / Decimal("0.05")) % 1 == 0


def test_rounding_never_makes_a_marketable_order_unmarketable():
    """A buy rounds up and a sell rounds down, so rounding moves the price
    further through the book rather than back out of it -- otherwise a
    marketable order could be left resting by a paisa."""
    buy = execution.marketable_limit(
        Decimal("2800"), side=OrderSide.BUY, buffer_pct=Decimal("0.0001")
    )
    sell = execution.marketable_limit(
        Decimal("2800"), side=OrderSide.SELL, buffer_pct=Decimal("0.0001")
    )
    assert buy >= Decimal("2800")
    assert sell <= Decimal("2800")


# ── derivatives contracts ────────────────────────────────────────────


def test_a_strategy_can_configure_one_contract_in_its_params():
    """A strategy that trades a single option should name it once, not on
    every signal. Without this an F&O signal could not be expressed at all --
    the adapter requires expiry, right and strike and refuses without them."""
    from datetime import date

    request = build(
        signal(symbol="NIFTY"),
        params={"expiry": "2026-09-29", "strike": "25000", "right": "call"},
        last=Decimal("245"),
    )
    assert request.expiry == date(2026, 9, 29)
    assert request.strike == Decimal("25000")
    assert request.right is OptionRight.CALL


def test_a_signal_can_pick_its_own_strike_per_trade():
    """A strategy that rolls strikes decides per signal, and that must beat
    whatever its params say."""
    from datetime import date

    request = build(
        signal(symbol="NIFTY", expiry=date(2026, 10, 27), strike=Decimal("26000"),
               right=OptionRight.PUT),
        params={"expiry": "2026-09-29", "strike": "25000", "right": "call"},
        last=Decimal("245"),
    )
    assert request.expiry == date(2026, 10, 27)
    assert request.strike == Decimal("26000")
    assert request.right is OptionRight.PUT


def test_a_futures_strategy_need_not_spell_out_its_right():
    """Breeze requires the field on every derivatives order and rejects an
    empty one, so "no strike" has to become OTHERS rather than None."""
    request = build(
        signal(symbol="CNXBAN"), params={"expiry": "2026-09-29"},
        last=Decimal("46230"),
    )
    assert request.right is OptionRight.OTHERS
    assert request.strike is None


def test_a_derivatives_order_defaults_to_carry_forward_not_a_cash_product():
    """The per-broker defaults describe equities -- CNC for Breeze, MIS for
    Kite. Either applied to an NFO order would ask for delivery of a
    contract."""
    request = build(
        signal(symbol="NIFTY"), params={"expiry": "2026-09-29"},
        last=Decimal("245"),
    )
    assert request.product is ProductType.NRML


def test_an_equity_order_is_unaffected():
    """The contract fields must stay absent for a cash order, or Breeze would
    be sent an expiry for a share."""
    request = build(signal(), last=Decimal("2800"))
    assert request.expiry is None
    assert request.strike is None
    assert request.right is None
    assert request.product is ProductType.CNC


def test_an_unreadable_expiry_is_refused_rather_than_ignored():
    """Silently dropping it would send the order as an equity trade, which
    Breeze would reject with a much vaguer message."""
    with pytest.raises(FeatureNotSupportedError, match="expiry date"):
        build(signal(symbol="NIFTY"), params={"expiry": "next thursday"},
              last=Decimal("245"))


def test_the_right_accepts_the_vocabularies_people_actually_write():
    """Breeze alone uses three different spellings across its own endpoints,
    and an LLM-written strategy may use any of them."""
    for written, expected in [
        ("CE", OptionRight.CALL), ("call", OptionRight.CALL),
        ("PE", OptionRight.PUT), ("put", OptionRight.PUT),
        ("fut", OptionRight.OTHERS), ("XX", OptionRight.OTHERS),
    ]:
        request = build(
            signal(symbol="NIFTY"),
            params={"expiry": "2026-09-29", "strike": "25000", "right": written},
            last=Decimal("245"),
        )
        assert request.right is expected, written


def test_an_unrecognised_right_is_refused():
    with pytest.raises(FeatureNotSupportedError, match="option right"):
        build(signal(symbol="NIFTY"),
              params={"expiry": "2026-09-29", "strike": "1", "right": "maybe"},
              last=Decimal("245"))
