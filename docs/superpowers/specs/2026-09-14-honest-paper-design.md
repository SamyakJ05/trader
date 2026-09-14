# Honest paper trading + backtester — design

Phase 2 of the program. Phase 1 (cross-user isolation, auth hardening, admin)
is merged to main.

## Why

Paper P&L is currently fiction in three ways, and each one flatters the
numbers:

- `estimate_charges` returns zero, so every trade looks free. On a ₹1L intraday
  round trip the real cost is roughly ₹60–100 in statutory charges alone.
- There is no holdings model. CNC buys sit in `positions` forever, so nothing
  distinguishes a delivery holding from an intraday position — and the two are
  taxed differently on sale (STT 0.1% on delivery sell vs 0.025% intraday).
- Cash is a latest-snapshot arithmetic rather than a ledger, so there is no
  audit trail of how a balance was reached.

Going live against a model that assumes zero fees means the first real trades
disagree with every number the platform ever showed. Fix the model first.

The backtester lands in the same phase because it shares the machinery: it
needs honest charges to be worth anything, and candles are also what the
strategy engine should be reading instead of raw ticks.

## Scope

**2a — honest costs**: charges engine, cash ledger, holdings + T+1, holiday
calendar.
**2b — candles and backtesting**: candle aggregation, historical import,
backtest engine.

## 1. Charges engine

Statutory components are broker-independent and go in one module. Brokerage is
per broker and per plan, so it hangs off an interface the adapters supply.

Equity charge stack, as of this writing (rates are configuration, not
constants buried in code — they change with budgets):

| Component | Delivery (CNC) | Intraday (MIS) |
|---|---|---|
| Brokerage | broker rule | broker rule |
| STT | 0.1% buy + 0.1% sell | 0.025% sell only |
| Exchange txn (NSE) | 0.00297% | 0.00297% |
| SEBI | ₹10 per crore | ₹10 per crore |
| Stamp duty | 0.015% buy | 0.003% buy |
| GST | 18% on (brokerage + exchange + SEBI) | same |

Zerodha's plan ships as the first implementation: ₹0 equity delivery, and
₹20 or 0.03% (whichever is lower) per executed order for intraday. Groww and
Breeze get their own rules when their adapters are verified; until then they
fall back to the Zerodha-shaped default with a note in the capability matrix.

`estimate_charges` is already wired into fill processing and cash movement
(`engines/paper/engine.py:130`), so this is a calculation change, not a
plumbing one. It gains `exchange` and a per-component breakdown so the UI can
show where the money went.

**Rounding**: every component rounds to paise (2dp) at the component level,
matching how contract notes are actually computed. Do not round only the total.

## 2. Cash as a ledger

`FundsSnapshot` keeps a running balance with no record of what moved it.
Replace with an append-only `cash_ledger` (entry type, amount, running balance,
order/fill reference), and derive the balance from the ledger.

Entry types: `OPENING`, `BUY`, `SELL`, `CHARGES`, `RESET`.

This is the same reasoning as the append-only audit log: a balance you cannot
explain is a balance you cannot trust, and reconciliation against a real broker
later needs the line items.

`FundsSnapshot` stays for broker-reported balances (live accounts report a
balance, not a ledger); the paper engine reads and writes the ledger.

## 3. Holdings and T+1 settlement

New `holdings` table, and a `pending_settlements` row per CNC buy fill.

- CNC buy fill → `pending_settlements` row with `settles_on` = next trading day.
- A worker job promotes matured rows into `holdings` (weighted average price).
- CNC sell checks holdings first; selling what has not settled is refused the
  way a real broker would refuse it.
- MIS is untouched — intraday positions never settle.

This is what makes the delivery/intraday STT distinction real rather than
cosmetic, and it is why the holiday calendar is in this phase: `settles_on`
needs to know which days are trading days.

## 4. Exchange holiday calendar

A static NSE holiday list per year (checked into the repo, a data file rather
than code), plus `is_trading_day()` and `next_trading_day()`.

Feeds both the market-hours guard (`engines/risk/engine.py:33` has the TODO)
and T+1 settlement. The list needs a yearly update; a test asserts the current
year is present so it fails loudly rather than silently treating a holiday as
a trading day.

## 5. Candle aggregation

New `candles` table (symbol, exchange, interval, ts, OHLCV).

A worker job aggregates 1m candles from the tick feed and rolls 1m into 5m.
The source is the simulator today and the Kite WebSocket after Phase 3 — the
backtester reads stored candles either way, so no rework when real data
arrives.

Strategies currently compute SMAs over raw ticks
(`engines/strategy/runner.py`), which is not what a crossover means. They move
to candles in this phase.

## 6. Historical import

A CLI that imports NSE daily and intraday history into `candles`, so the
backtester has real past data rather than only what the instance has observed.

Source: yfinance (`RELIANCE.NS` style symbols), which is vendored in the
skills repo. Two caveats to record in the README rather than discover later:
it scrapes Yahoo's unofficial endpoints and can break without notice, and
Yahoo's terms describe personal use — fine for the operator's own backtesting,
worth revisiting if this is ever offered to others.

Imported candles are marked with their source so a backtest can say what it
ran on.

## 7. Backtest engine

Runs a registered strategy over stored candles and reports what it would have
done.

- Reuses `STRATEGY_REGISTRY`, so anything runnable live is backtestable and
  the two cannot drift.
- Simulates fills at the next candle's open (not the signal candle's close —
  that is lookahead, and it is the single most common way a backtest lies).
- Applies the real charges engine to every simulated fill.
- Reports: total return, max drawdown, win rate, trade count, total charges,
  and an equity curve.
- Results stored in `backtest_runs` so they can be compared and, in Phase 5,
  shown to the AI before a generated strategy is ever started.

**Explicitly not modelled**, and stated in the UI so results are not
overtrusted: slippage beyond the next-open assumption, market impact,
liquidity limits, and circuit breakers.

## Data changes

Migration 0008: `cash_ledger`, `holdings`, `pending_settlements`, `candles`,
`backtest_runs`.

Existing paper accounts get an `OPENING` ledger entry derived from their
latest funds snapshot, so balances carry over rather than resetting.

## Testing

TDD throughout, matching the existing pure-unit style.

- Charges: each component against hand-computed contract-note figures, for
  delivery and intraday, buy and sell. These are arithmetic with known
  answers, so they are worth pinning exactly.
- Ledger: balance always equals the sum of entries; a reset produces a fresh
  opening entry.
- Settlement: T+1 crosses a weekend correctly; crosses a holiday correctly;
  selling unsettled stock is refused.
- Holiday calendar: current-year list present; a known holiday is not a
  trading day.
- Candles: aggregation boundaries (a tick exactly on a minute edge belongs to
  the new candle); 1m→5m rollup.
- Backtest: **a strategy that would be profitable with lookahead must not be
  profitable without it** — this is the test that proves the fill model is
  honest.

## Open question deferred

Charges rates change with government budgets. This phase hardcodes current
rates in a config module with a dated comment. If the platform runs long
enough for rates to change mid-year, historical fills would need
rate-versioning by date — noted, not built.


## Implementation notes (2026-09-14)

The remainder of 2a and 2b is implemented in migration 0008 and the paper,
market-data and backtest modules. The cash ledger is append-only at the DB
level; account locks serialize fills, resets and settlement. Paper read paths
use it directly. Positive legacy CNC positions migrate as pending for one
session; negative legacy CNC inventory requires a reset before upgrade.

The backtest UI/API exposes registered SMA crossover. The engine reuses the
strategy interface but explicitly rejects async/AI replay, short entries and
reversals. AI replay needs historical context and recorded decisions; silently
using today's LLM context would not be a historical test. Model limits also
include no MIS session-close square-off, historical charge-rate versioning,
corporate-action handling or liquidity model. The existing paper plan retains
zero brokerage plus statutory/DP charges. Every result records these limits,
its source and a hash of the candles used. See README for commands and routes.

The rates in the original design table above were superseded by the verified
charges implementation and `engines/paper/rates.py`; that module remains the
source of truth. The original holiday verification caveats remain open.
