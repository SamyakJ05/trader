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

from decimal import Decimal

RATES_EFFECTIVE_FROM = "2026-03-01"

# ── Securities Transaction Tax ───────────────────────────────────────
# Delivery is charged on both legs; intraday on the sell leg only.
STT_DELIVERY_BUY = Decimal("0.001")      # 0.1%
STT_DELIVERY_SELL = Decimal("0.001")     # 0.1%
STT_INTRADAY_BUY = Decimal("0")          # nil
STT_INTRADAY_SELL = Decimal("0.00025")   # 0.025%

# ── Exchange transaction charges ─────────────────────────────────────
# Same rate for delivery and intraday; charged on both legs. Split into its
# two published components so the breakdown matches a contract note, though
# the combined 0.00307% is what a broker's calculator displays.
NSE_TXN_CHARGE = Decimal("0.0000306990")     # Rs 306.99 per crore
NSE_IPFT_CHARGE = Decimal("0.000000001")     # Rs 0.01 per crore (0.01 / 1e7)
BSE_TXN_CHARGE = Decimal("0.0000375")        # BSE differs; unused until we route there

# ── SEBI turnover fee ────────────────────────────────────────────────
# Rs 10 per crore = 10 / 1e7 = 0.000001 as a fraction of turnover (0.0001%).
# The percentage and the fraction differ by 100x; conflating them overstates
# this charge by two orders of magnitude.
SEBI_TURNOVER_FEE = Decimal("0.000001")

# ── Stamp duty (buy side only) ───────────────────────────────────────
STAMP_DELIVERY_BUY = Decimal("0.00015")  # 0.015%
STAMP_INTRADAY_BUY = Decimal("0.00003")  # 0.003%

# ── GST ──────────────────────────────────────────────────────────────
GST_RATE = Decimal("0.18")

# ── Depository participant charges ───────────────────────────────────
# Flat per scrip per day on delivery SELL, regardless of quantity: selling 10
# shares costs the same as selling 10,000. Triggered by the demat debit, so
# never charged on a buy or on intraday.
#
# Rs 15.34 = Rs 3.50 CDSL + Rs 9.50 broker + Rs 2.34 GST. The GST is already
# inside this figure, so it must NOT be added to the GST base again.
DP_CHARGE_PER_SCRIP = Decimal("15.34")
# CDSL discounts Rs 0.25 for a female first holder; not modelled (we do not
# hold that attribute), but noted so the difference is explainable.
