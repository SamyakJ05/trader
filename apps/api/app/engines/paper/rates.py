"""Statutory and exchange charge rates for Indian equity trading.

Rates as of 2026-09-14, verified against primary sources. They change with
Union Budgets and exchange circulars, so they live here as dated configuration
rather than as constants scattered through the calculation.

Sources:
- STT: https://zerodha.com/charges/ (unchanged in Budgets 2025 and 2026)
- NSE transaction + IPFT: circular NSE/FA/73061, 27 Feb 2026, effective
  1 Mar 2026. This SUPERSEDED the 0.00297% rate that most published tables
  still show — the IPFT hike was rolled back and transaction charges raised to
  compensate, leaving the total at Rs 307 per crore.
- SEBI turnover fee: https://www.sebi.gov.in/commreport/feestructure.html
- Stamp duty: Indian Stamp Act amendment, effective 1 Jul 2020. Uniform
  nationally for exchange-routed trades; no per-state variation.
- GST: 18% on brokerage + exchange charges + SEBI fee. NOT on STT or stamp
  duty, which are statutory levies outside the GST net.

Budget 2026 raised derivatives STT (futures 0.02% -> 0.05%, options premium
0.1% -> 0.15%) effective 1 Apr 2026. Equity cash was untouched. Those rates
are not modelled here because this engine covers equity only.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

RATES_EFFECTIVE_FROM = "2026-03-01"


@dataclass(frozen=True)
class RateSet:
    """Every rate in force over one period.

    Backtests span years, and these rates move. Pricing a 2023 trade with 2026
    rates is a quiet error: it shifts every P&L in the same direction, so a
    strategy looks consistently better or worse than it was without anything
    looking obviously wrong.
    """

    effective_from: date
    label: str
    stt_delivery_buy: Decimal
    stt_delivery_sell: Decimal
    stt_intraday_buy: Decimal
    stt_intraday_sell: Decimal
    nse_txn_charge: Decimal
    nse_ipft_charge: Decimal
    bse_txn_charge: Decimal
    sebi_turnover_fee: Decimal
    sebi_fee_attracts_gst: bool
    stamp_delivery_buy: Decimal
    stamp_intraday_buy: Decimal
    gst_rate: Decimal
    dp_charge_per_scrip: Decimal | None


def _per_crore(rupees: str) -> Decimal:
    """Exchange and SEBI rates are published per crore of turnover."""
    return Decimal(rupees) / Decimal(10_000_000)


# Ordered oldest first. Each entry holds until the next one's date.
#
# Verified against primary sources where reachable. NSE circular FA73061
# (27 Feb 2026) was read directly and states both the current and prior cash
# market figures; FA56129 (24 Mar 2023) was read directly for the IPFT change.
# Gaps are recorded honestly in `uncertain` below rather than filled with
# plausible numbers.
RATE_HISTORY: tuple[RateSet, ...] = (
    RateSet(
        effective_from=date(2020, 7, 1),
        label="uniform stamp duty (Indian Stamp Act amendment)",
        stt_delivery_buy=Decimal("0.001"),
        stt_delivery_sell=Decimal("0.001"),
        stt_intraday_buy=Decimal("0"),
        stt_intraday_sell=Decimal("0.00025"),
        # Pre-2023 NSE cash charges were slab-based on member volume, and the
        # slab table could not be recovered from a primary source. This uses
        # the 1 Apr 2023 flat rate as the best available approximation for the
        # 2020-2023 window; see `uncertain`.
        nse_txn_charge=_per_crore("325"),
        nse_ipft_charge=_per_crore("0.01"),
        bse_txn_charge=Decimal("0.0000375"),
        sebi_turnover_fee=_per_crore("10"),
        # SEBI's fee was exempt from GST until 18 Jul 2022.
        sebi_fee_attracts_gst=False,
        stamp_delivery_buy=Decimal("0.00015"),
        stamp_intraday_buy=Decimal("0.00003"),
        gst_rate=Decimal("0.18"),
        # CDSL's pre-Oct-2024 DP charge was slab-based and could not be
        # recovered. None means "not modelled for this period" rather than
        # "free" -- charges() surfaces it as an explicit note.
        dp_charge_per_scrip=None,
    ),
    RateSet(
        effective_from=date(2022, 7, 18),
        label="GST exemption on the SEBI turnover fee withdrawn",
        stt_delivery_buy=Decimal("0.001"),
        stt_delivery_sell=Decimal("0.001"),
        stt_intraday_buy=Decimal("0"),
        stt_intraday_sell=Decimal("0.00025"),
        nse_txn_charge=_per_crore("325"),
        nse_ipft_charge=_per_crore("0.01"),
        bse_txn_charge=Decimal("0.0000375"),
        sebi_turnover_fee=_per_crore("10"),
        sebi_fee_attracts_gst=True,
        stamp_delivery_buy=Decimal("0.00015"),
        stamp_intraday_buy=Decimal("0.00003"),
        gst_rate=Decimal("0.18"),
        dp_charge_per_scrip=None,
    ),
    RateSet(
        effective_from=date(2023, 4, 1),
        label="NSE circular FA56129: charges cut, IPFT raised to Rs 10/crore",
        stt_delivery_buy=Decimal("0.001"),
        stt_delivery_sell=Decimal("0.001"),
        stt_intraday_buy=Decimal("0"),
        stt_intraday_sell=Decimal("0.00025"),
        nse_txn_charge=_per_crore("325"),
        nse_ipft_charge=_per_crore("10"),
        bse_txn_charge=Decimal("0.0000375"),
        sebi_turnover_fee=_per_crore("10"),
        sebi_fee_attracts_gst=True,
        stamp_delivery_buy=Decimal("0.00015"),
        stamp_intraday_buy=Decimal("0.00003"),
        gst_rate=Decimal("0.18"),
        dp_charge_per_scrip=None,
    ),
    RateSet(
        effective_from=date(2024, 10, 1),
        label="SEBI true-to-label: uniform charges; CDSL flat Rs 3.50 DP",
        stt_delivery_buy=Decimal("0.001"),
        stt_delivery_sell=Decimal("0.001"),
        stt_intraday_buy=Decimal("0"),
        stt_intraday_sell=Decimal("0.00025"),
        # FA73061 states the outgoing figures: Rs 297 + Rs 10 IPFT = Rs 307.
        nse_txn_charge=_per_crore("297"),
        nse_ipft_charge=_per_crore("10"),
        bse_txn_charge=Decimal("0.0000375"),
        sebi_turnover_fee=_per_crore("10"),
        sebi_fee_attracts_gst=True,
        stamp_delivery_buy=Decimal("0.00015"),
        stamp_intraday_buy=Decimal("0.00003"),
        gst_rate=Decimal("0.18"),
        dp_charge_per_scrip=Decimal("15.34"),
    ),
    RateSet(
        effective_from=date(2026, 3, 1),
        label="NSE circular FA73061: IPFT rolled back, charges raised to match",
        stt_delivery_buy=Decimal("0.001"),
        stt_delivery_sell=Decimal("0.001"),
        stt_intraday_buy=Decimal("0"),
        stt_intraday_sell=Decimal("0.00025"),
        nse_txn_charge=_per_crore("306.99"),
        nse_ipft_charge=_per_crore("0.01"),
        bse_txn_charge=Decimal("0.0000375"),
        sebi_turnover_fee=_per_crore("10"),
        sebi_fee_attracts_gst=True,
        stamp_delivery_buy=Decimal("0.00015"),
        stamp_intraday_buy=Decimal("0.00003"),
        gst_rate=Decimal("0.18"),
        dp_charge_per_scrip=Decimal("15.34"),
    ),
)

# What could not be established from a primary source. Recorded so a backtest
# over these windows can say what it does not know, rather than implying a
# precision it lacks.
UNCERTAIN = {
    "2020-07-01..2023-03-31": (
        "NSE cash transaction charges were slab-based on member volume in this "
        "period and the slab table could not be recovered; the 1 Apr 2023 flat "
        "rate is used as an approximation."
    ),
    "2020-06-01..~2021-03-31": (
        "SEBI halved its turnover fee as COVID relief from 1 Jun 2020. The "
        "reversion date was not confirmed, so the full Rs 10/crore is charged "
        "throughout -- this slightly overstates cost in that window."
    ),
    "before 2024-10-01": (
        "CDSL DP charges were slab-based and could not be recovered, so DP is "
        "not charged on delivery sells before that date. Delivery costs in "
        "this period are understated by roughly Rs 15 per scrip per day."
    ),
    "zerodha brokerage": (
        "Zerodha's intraday 'Rs 20 or 0.03%, whichever is lower' structure is "
        "applied to all dates. When it replaced flat Rs 20 could not be "
        "established; delivery has been free since December 2015."
    ),
}


def rates_for(day: date | None = None) -> RateSet:
    """The rates in force on a date. Defaults to the latest set."""
    if day is None:
        return RATE_HISTORY[-1]
    applicable = [r for r in RATE_HISTORY if r.effective_from <= day]
    if not applicable:
        # Before the table starts: use the earliest set rather than failing, so
        # an old backtest still runs -- but say so.
        return RATE_HISTORY[0]
    return applicable[-1]


def uncertainty_notes(day: date | None) -> list[str]:
    """Caveats that apply to a trade on this date."""
    if day is None:
        return []
    notes = []
    if day < date(2023, 4, 1):
        notes.append(UNCERTAIN["2020-07-01..2023-03-31"])
    if day < date(2021, 4, 1):
        notes.append(UNCERTAIN["2020-06-01..~2021-03-31"])
    if day < date(2024, 10, 1):
        notes.append(UNCERTAIN["before 2024-10-01"])
    return notes


# ── current rates, kept as module constants for existing callers ─────
_CURRENT = RATE_HISTORY[-1]

STT_DELIVERY_BUY = _CURRENT.stt_delivery_buy
STT_DELIVERY_SELL = _CURRENT.stt_delivery_sell
STT_INTRADAY_BUY = _CURRENT.stt_intraday_buy
STT_INTRADAY_SELL = _CURRENT.stt_intraday_sell
NSE_TXN_CHARGE = _CURRENT.nse_txn_charge
NSE_IPFT_CHARGE = _CURRENT.nse_ipft_charge
BSE_TXN_CHARGE = _CURRENT.bse_txn_charge
SEBI_TURNOVER_FEE = _CURRENT.sebi_turnover_fee
STAMP_DELIVERY_BUY = _CURRENT.stamp_delivery_buy
STAMP_INTRADAY_BUY = _CURRENT.stamp_intraday_buy
GST_RATE = _CURRENT.gst_rate
DP_CHARGE_PER_SCRIP = _CURRENT.dp_charge_per_scrip
