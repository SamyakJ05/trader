"""Indian equity charge calculation.

These are arithmetic with known answers, so they are pinned exactly rather
than approximately: a charge engine that is roughly right produces a P&L that
is quietly wrong, which is the thing this phase exists to eliminate.

Reference figures are hand-computed from the rates in rates.py. Where a
published broker calculator would differ by a paisa, that is rounding policy,
noted in the relevant test.
"""

from decimal import Decimal

import pytest

from app.domain.enums import Broker, Exchange, OrderSide, ProductType
from app.engines.paper import rates
from app.engines.paper.charges import compute_charges, estimate_charges


def buy(**kw):
    defaults = dict(
        broker=Broker.ZERODHA,
        side=OrderSide.BUY,
        product=ProductType.CNC,
        quantity=100,
        price=Decimal("1000"),
    )
    defaults.update(kw)
    return compute_charges(**defaults)


def sell(**kw):
    return buy(side=OrderSide.SELL, **kw)


# ── rate constants ───────────────────────────────────────────────────
# Rates published per crore are easy to encode 100x or 10x wrong as a
# fraction. These assertions restate each one at its published scale, so a
# units slip fails here rather than silently distorting every P&L figure.

ONE_CRORE = Decimal("10000000")


def test_per_crore_rates_match_their_published_figures():
    assert ONE_CRORE * rates.NSE_TXN_CHARGE == Decimal("306.99")
    assert ONE_CRORE * rates.NSE_IPFT_CHARGE == Decimal("0.01")
    assert ONE_CRORE * (rates.NSE_TXN_CHARGE + rates.NSE_IPFT_CHARGE) == Decimal("307.00")
    assert ONE_CRORE * rates.SEBI_TURNOVER_FEE == Decimal("10.00")
    assert ONE_CRORE * rates.BSE_TXN_CHARGE == Decimal("375.00")


def test_percentage_rates_match_their_published_figures():
    assert rates.STT_DELIVERY_BUY * 100 == Decimal("0.1")
    assert rates.STT_DELIVERY_SELL * 100 == Decimal("0.1")
    assert rates.STT_INTRADAY_SELL * 100 == Decimal("0.025")
    assert rates.STT_INTRADAY_BUY == Decimal("0")
    assert rates.STAMP_DELIVERY_BUY * 100 == Decimal("0.015")
    assert rates.STAMP_INTRADAY_BUY * 100 == Decimal("0.003")
    assert rates.GST_RATE * 100 == Decimal("18")


# ── STT ──────────────────────────────────────────────────────────────


def test_delivery_stt_is_charged_on_both_legs():
    """0.1% each way on a Rs 1,00,000 turnover = Rs 100."""
    assert buy(product=ProductType.CNC).stt == Decimal("100.00")
    assert sell(product=ProductType.CNC).stt == Decimal("100.00")


def test_intraday_stt_is_sell_side_only():
    """The buy leg is free; the sell leg pays 0.025% = Rs 25."""
    assert buy(product=ProductType.MIS).stt == Decimal("0.00")
    assert sell(product=ProductType.MIS).stt == Decimal("25.00")


# ── exchange transaction charges ─────────────────────────────────────


def test_exchange_charge_uses_the_current_nse_rate():
    """NSE circular FA73061 (effective 1 Mar 2026): Rs 306.99/crore plus
    Rs 0.01/crore IPFT = Rs 307/crore. On Rs 1,00,000 that is Rs 3.07.

    The widely-republished 0.00297% predates that circular and would give
    Rs 2.97 — this test is what stops that number creeping back in.
    """
    assert buy().exchange_txn == Decimal("3.07")
    assert sell().exchange_txn == Decimal("3.07")


def test_exchange_charge_is_the_same_for_delivery_and_intraday():
    assert buy(product=ProductType.CNC).exchange_txn == buy(
        product=ProductType.MIS
    ).exchange_txn


def test_bse_has_its_own_rate():
    """0.00375% on Rs 1,00,000 = Rs 3.75. Do not reuse the NSE constant."""
    assert buy(exchange=Exchange.BSE).exchange_txn == Decimal("3.75")


# ── SEBI turnover fee ────────────────────────────────────────────────


def test_sebi_fee_is_ten_rupees_per_crore():
    """Rs 10/crore on Rs 1,00,000 = Rs 0.10."""
    assert buy().sebi == Decimal("0.10")


# ── stamp duty ───────────────────────────────────────────────────────


def test_stamp_duty_is_buy_side_only():
    assert sell(product=ProductType.CNC).stamp_duty == Decimal("0.00")
    assert sell(product=ProductType.MIS).stamp_duty == Decimal("0.00")


def test_delivery_stamp_duty():
    """0.015% on Rs 1,00,000 = Rs 15."""
    assert buy(product=ProductType.CNC).stamp_duty == Decimal("15.00")


def test_intraday_stamp_duty():
    """0.003% on Rs 1,00,000 = Rs 3."""
    assert buy(product=ProductType.MIS).stamp_duty == Decimal("3.00")


# ── brokerage ────────────────────────────────────────────────────────


def test_zerodha_delivery_is_free():
    assert buy(product=ProductType.CNC).brokerage == Decimal("0.00")


def test_zerodha_intraday_takes_the_lower_of_percentage_and_cap():
    """Rs 1,00,000 at 0.03% = Rs 30, above the Rs 20 cap, so Rs 20."""
    assert buy(product=ProductType.MIS).brokerage == Decimal("20.00")


def test_small_intraday_order_pays_the_percentage_not_the_cap():
    """Rs 10,000 at 0.03% = Rs 3, below the cap."""
    small = compute_charges(
        broker=Broker.ZERODHA,
        side=OrderSide.BUY,
        product=ProductType.MIS,
        quantity=10,
        price=Decimal("1000"),
    )
    assert small.brokerage == Decimal("3.00")


def test_paper_broker_charges_no_brokerage_but_still_charges_statutory():
    """The simulator has no broker taking a cut, but the government's share is
    what makes paper P&L honest."""
    paper = compute_charges(
        broker=Broker.PAPER,
        side=OrderSide.SELL,
        product=ProductType.CNC,
        quantity=100,
        price=Decimal("1000"),
    )
    assert paper.brokerage == Decimal("0.00")
    assert paper.stt == Decimal("100.00")
    assert paper.total > Decimal("100")


# ── GST ──────────────────────────────────────────────────────────────


def test_gst_applies_to_brokerage_exchange_and_sebi_only():
    """18% of (20.00 brokerage + 3.07 exchange + 0.10 SEBI) = Rs 4.17."""
    intraday = buy(product=ProductType.MIS)
    base = intraday.brokerage + intraday.exchange_txn + intraday.sebi
    assert base == Decimal("23.17")
    assert intraday.gst == Decimal("4.17")


def test_gst_excludes_stt_and_stamp_duty():
    """Both are statutory levies outside the GST net. If they were included,
    a delivery buy's GST would be far larger than this."""
    delivery = buy(product=ProductType.CNC)
    assert delivery.stt == Decimal("100.00")
    assert delivery.stamp_duty == Decimal("15.00")
    # Only exchange (3.07) + SEBI (0.10) are in the base; brokerage is nil.
    assert delivery.gst == Decimal("0.57")


# ── DP charges ───────────────────────────────────────────────────────


def test_dp_charge_applies_to_delivery_sells_only():
    assert sell(product=ProductType.CNC).dp_charges == rates.DP_CHARGE_PER_SCRIP
    assert buy(product=ProductType.CNC).dp_charges == Decimal("0")
    assert sell(product=ProductType.MIS).dp_charges == Decimal("0")


def test_dp_charge_is_flat_regardless_of_quantity():
    """Selling 10 shares costs the same as selling 10,000 — it is a per-scrip
    demat debit fee, not a percentage."""
    small = compute_charges(
        broker=Broker.ZERODHA,
        side=OrderSide.SELL,
        product=ProductType.CNC,
        quantity=10,
        price=Decimal("1000"),
    )
    large = compute_charges(
        broker=Broker.ZERODHA,
        side=OrderSide.SELL,
        product=ProductType.CNC,
        quantity=10000,
        price=Decimal("1000"),
    )
    assert small.dp_charges == large.dp_charges == rates.DP_CHARGE_PER_SCRIP


def test_dp_charge_is_once_per_scrip_per_day():
    """A second sell of the same scrip on the same day does not pay again."""
    second = sell(product=ProductType.CNC, is_first_sell_of_scrip_today=False)
    assert second.dp_charges == Decimal("0")


def test_dp_charge_is_not_taxed_again():
    """Its published figure already contains Rs 2.34 of GST; adding it to the
    GST base would tax it twice."""
    delivery_sell = sell(product=ProductType.CNC)
    without_dp = sell(product=ProductType.CNC, is_first_sell_of_scrip_today=False)
    assert delivery_sell.gst == without_dp.gst


# ── totals ───────────────────────────────────────────────────────────


def test_delivery_buy_total():
    """0 brokerage + 100 STT + 3.07 exchange + 0.10 SEBI + 15 stamp
    + 0.57 GST = Rs 118.74."""
    assert buy(product=ProductType.CNC).total == Decimal("118.74")


def test_intraday_round_trip_is_materially_expensive():
    """The point of the whole engine: a Rs 1L intraday round trip is not free.
    Buy 20 + 0 + 3.07 + 0.10 + 3 + 4.17 = 30.34;
    sell 20 + 25 + 3.07 + 0.10 + 0 + 4.17 = 52.34. Total Rs 82.68."""
    in_ = buy(product=ProductType.MIS).total
    out = sell(product=ProductType.MIS).total
    assert in_ == Decimal("30.34")
    assert out == Decimal("52.34")
    assert in_ + out == Decimal("82.68")


def test_estimate_charges_matches_the_breakdown_total():
    """The existing fill pipeline calls estimate_charges; it must not drift
    from the detailed calculation."""
    total = estimate_charges(
        OrderSide.SELL,
        ProductType.CNC,
        100,
        Decimal("1000"),
        broker=Broker.ZERODHA,
    )
    assert total == sell(product=ProductType.CNC).total


# ── edges ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("quantity,price", [(0, Decimal("100")), (10, Decimal("0"))])
def test_zero_quantity_or_price_costs_nothing(quantity, price):
    result = compute_charges(
        broker=Broker.ZERODHA,
        side=OrderSide.BUY,
        product=ProductType.CNC,
        quantity=quantity,
        price=price,
    )
    assert result.total == Decimal("0")


def test_every_component_is_rounded_to_paise():
    result = compute_charges(
        broker=Broker.ZERODHA,
        side=OrderSide.SELL,
        product=ProductType.MIS,
        quantity=37,
        price=Decimal("1234.56"),
    )
    for value in (
        result.brokerage,
        result.stt,
        result.exchange_txn,
        result.sebi,
        result.stamp_duty,
        result.gst,
        result.total,
    ):
        assert value == value.quantize(Decimal("0.01")), f"{value} is not paise-rounded"


def test_breakdown_serialises_with_its_rate_vintage():
    """Rates change with budgets and circulars; a stored charge should say
    which set produced it."""
    payload = buy().as_dict()
    assert payload["rates_effective_from"] == rates.RATES_EFFECTIVE_FROM
    assert Decimal(payload["total"]) == buy().total


# ── date-versioned rates ─────────────────────────────────────────────
# Backtests span years and these rates move. Pricing a 2023 trade with 2026
# rates shifts every P&L the same way, so a strategy looks consistently better
# or worse than it was without anything looking obviously wrong.


def test_rates_resolve_by_trade_date():
    from datetime import date as _date

    assert rates.rates_for(_date(2021, 1, 1)).effective_from == _date(2020, 7, 1)
    assert rates.rates_for(_date(2023, 6, 1)).effective_from == _date(2023, 4, 1)
    assert rates.rates_for(_date(2025, 1, 1)).effective_from == _date(2024, 10, 1)
    assert rates.rates_for(_date(2026, 9, 15)).effective_from == _date(2026, 3, 1)


def test_no_date_uses_current_rates():
    """Live trading passes no date and must get today's rates."""
    assert rates.rates_for(None) is rates.RATE_HISTORY[-1]


def test_ipft_rollback_is_reflected_across_the_2026_boundary():
    """NSE circular FA73061: IPFT went Rs 10 -> Rs 0.01 per crore and
    transaction charges rose to compensate, leaving the total at Rs 307."""
    from datetime import date as _date

    before = rates.rates_for(_date(2026, 2, 28))
    after = rates.rates_for(_date(2026, 3, 1))
    crore = Decimal("10000000")
    assert crore * before.nse_ipft_charge == Decimal("10.00")
    assert crore * after.nse_ipft_charge == Decimal("0.01")
    # The member's total outflow is unchanged, which is the circular's point.
    assert crore * (before.nse_txn_charge + before.nse_ipft_charge) == Decimal("307.00")
    assert crore * (after.nse_txn_charge + after.nse_ipft_charge) == Decimal("307.00")


def test_sebi_fee_was_outside_the_gst_base_before_july_2022():
    """The exemption was withdrawn on 18 Jul 2022. Before that, GST applied to
    brokerage and exchange charges only."""
    from datetime import date as _date

    before = compute_charges(
        broker=Broker.ZERODHA,
        side=OrderSide.BUY,
        product=ProductType.MIS,
        quantity=100,
        price=Decimal("1000"),
        on=_date(2022, 7, 1),
    )
    after = compute_charges(
        broker=Broker.ZERODHA,
        side=OrderSide.BUY,
        product=ProductType.MIS,
        quantity=100,
        price=Decimal("1000"),
        on=_date(2022, 8, 1),
    )
    assert before.gst < after.gst


def test_dp_charge_is_not_applied_before_cdsl_went_flat():
    """CDSL's pre-Oct-2024 slab rates could not be recovered, so DP is not
    charged then — and the breakdown says so rather than implying it was free."""
    from datetime import date as _date

    old = compute_charges(
        broker=Broker.ZERODHA,
        side=OrderSide.SELL,
        product=ProductType.CNC,
        quantity=100,
        price=Decimal("1000"),
        on=_date(2024, 6, 1),
    )
    assert old.dp_charges == Decimal("0")
    assert any("not modelled" in note for note in old.notes)


def test_a_historical_trade_is_priced_differently_from_today():
    """The whole point: the same trade in 2021 and 2026 does not cost the same."""
    from datetime import date as _date

    old = compute_charges(
        broker=Broker.ZERODHA,
        side=OrderSide.BUY,
        product=ProductType.MIS,
        quantity=100,
        price=Decimal("1000"),
        on=_date(2021, 1, 1),
    )
    now = compute_charges(
        broker=Broker.ZERODHA,
        side=OrderSide.BUY,
        product=ProductType.MIS,
        quantity=100,
        price=Decimal("1000"),
        on=_date(2026, 9, 15),
    )
    assert old.total != now.total


def test_the_breakdown_names_the_rate_period_it_used():
    from datetime import date as _date

    payload = compute_charges(
        broker=Broker.ZERODHA,
        side=OrderSide.BUY,
        product=ProductType.CNC,
        quantity=10,
        price=Decimal("100"),
        on=_date(2023, 6, 1),
    ).as_dict()
    assert payload["rates_effective_from"] == "2023-04-01"
    assert "FA56129" in payload["rates_label"]


# ── placeholder brokerage is declared, not silent ────────────────────


def test_breeze_brokerage_is_flagged_as_a_placeholder():
    """ICICI Direct is a full-service broker whose percentage rates with a
    per-order floor are nothing like Zerodha's flat cap. Until its real plan is
    encoded, every P&L on a Breeze account is wrong in the same direction —
    and a consistent bias is what survives a glance at the numbers."""
    from app.engines.paper.brokerage import is_placeholder

    assert is_placeholder(Broker.ICICI_BREEZE)
    assert not is_placeholder(Broker.ZERODHA)
    assert not is_placeholder(Broker.PAPER)


def test_a_placeholder_says_so_on_the_breakdown():
    breakdown = compute_charges(
        broker=Broker.ICICI_BREEZE,
        side=OrderSide.BUY,
        product=ProductType.CNC,
        quantity=100,
        price=Decimal("1000"),
    )
    assert any("placeholder" in note for note in breakdown.notes)


def test_a_real_plan_carries_no_placeholder_note():
    breakdown = compute_charges(
        broker=Broker.ZERODHA,
        side=OrderSide.BUY,
        product=ProductType.CNC,
        quantity=100,
        price=Decimal("1000"),
    )
    assert not any("placeholder" in note for note in breakdown.notes)
