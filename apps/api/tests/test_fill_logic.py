"""Characterization tests for the paper engine's fill-price matrix."""

from decimal import Decimal
from types import SimpleNamespace

from app.engines.paper.engine import SLIPPAGE_BPS, _fill_price, _slip

D = Decimal


def order(side: str, otype: str, price=None, trigger=None):
    return SimpleNamespace(side=side, order_type=otype, price=price, trigger_price=trigger)


def test_market_buy_slips_against_buyer():
    px = _fill_price(order("BUY", "MARKET"), D("100"))
    assert px == D("100") * (1 + SLIPPAGE_BPS / 10000)


def test_market_sell_slips_against_seller():
    px = _fill_price(order("SELL", "MARKET"), D("100"))
    assert px == D("99.95")


def test_limit_buy_fills_at_or_below_limit():
    o = order("BUY", "LIMIT", price=D("100"))
    assert _fill_price(o, D("101")) is None
    assert _fill_price(o, D("99")) == D("99")  # never worse than limit
    assert _fill_price(o, D("100")) == D("100")


def test_limit_sell_fills_at_or_above_limit():
    o = order("SELL", "LIMIT", price=D("100"))
    assert _fill_price(o, D("99")) is None
    assert _fill_price(o, D("101")) == D("101")


def test_sl_buy_arms_on_trigger_then_limit():
    o = order("BUY", "SL", price=D("106"), trigger=D("105"))
    assert _fill_price(o, D("104")) is None  # not armed
    assert _fill_price(o, D("105.5")) == D("105.5")  # armed, within limit
    assert _fill_price(o, D("107")) is None  # armed but above limit


def test_sl_m_sell_arms_then_fills_market():
    o = order("SELL", "SL_M", trigger=D("95"))
    assert _fill_price(o, D("96")) is None
    px = _fill_price(o, D("94"))
    assert px is not None and px < D("94")  # market fill with sell-side slippage


def test_slip_rounding_two_decimals():
    assert _slip(D("123.456"), "BUY").as_tuple().exponent == -2
