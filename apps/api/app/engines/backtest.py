"""Deterministic next-open execution over completed, single-source candles."""

from decimal import Decimal

from app.domain.calendar import IST, is_intraday_square_off_due, settlement_date
from app.domain.enums import Broker, OrderSide, ProductType, SignalType
from app.engines.paper.charges import compute_charges
from app.engines.metrics import summarise as summarise_metrics
from app.engines.paper.ledger import money
from app.engines.paper.pnl import apply_fill
from app.engines.strategy.base import AsyncStrategyBase, StrategyContext

LIMITATIONS = [
    "Fills use the next stored candle open; missing bars are not synthesized.",
    "No additional slippage, market impact, liquidity limits or circuit breakers.",
    "Current charge rates apply to all dates; historical rate changes are not modeled.",
    "MIS positions are squared off at the broker cutoff (15:20 IST) or, when "
    "no bar reaches it, at the next day's open -- not at an exact 15:20 price.",
    "Long-only; short entries and reversals are rejected because margin is not modeled.",
    "The paper brokerage plan charges zero brokerage; statutory and delivery DP charges apply.",
    "Unadjusted Yahoo data does not model splits, dividends or other corporate actions.",
    "Open positions are marked at the last close, not forcibly liquidated.",
    "The final bar's signal is dropped: there is no later bar to fill it at.",
    "Backtest accounting uses one average-cost ledger; the live engine keeps "
    "separate position and settled-holding averages, so delivery P&L can differ.",
    "5m candles are dropped, never part-formed, when a 1m bar is missing from "
    "the bucket -- so gaps appear rather than fabricated prices.",
    "A zero-P&L round trip counts as a loss in win rate and is invisible to "
    "profit factor.",
    "AI strategies cannot be replayed without historical context and recorded decisions.",
]


def run_backtest(
    impl,
    candles,
    *,
    symbol,
    exchange="NSE",
    product="MIS",
    initial_cash=Decimal("1000000"),
    params=None,
    interval="1d",
    broker=Broker.PAPER,
):
    """`broker` decides whose brokerage the simulated fills pay.

    Defaulting to PAPER charges statutory costs but no broker's cut, which
    flatters any strategy meant for a real account — ICICI's percentage
    brokerage alone is Rs 440 on a Rs 1 lakh delivery round trip. Pass the
    broker the strategy would actually trade through.
    """
    if isinstance(impl, AsyncStrategyBase):
        raise ValueError(
            "AI/async strategies require recorded historical decisions and are not replayable"
        )
    if not candles:
        raise ValueError("No candles in selected range")
    if initial_cash <= 0 or not initial_cash.is_finite():
        raise ValueError("Initial cash must be finite and positive")
    product = ProductType(product)
    if product not in (ProductType.MIS, ProductType.CNC):
        raise ValueError("Only MIS and CNC equity backtests are supported")
    for i, bar in enumerate(candles):
        if bar.ts.tzinfo is None:
            raise ValueError("Candle timestamp requires timezone")
        if i and bar.ts <= candles[i - 1].ts:
            raise ValueError("Candle timestamps must be strictly increasing")
        if any(not p.is_finite() or p <= 0 for p in (bar.open, bar.high, bar.low, bar.close)):
            raise ValueError("Candle prices must be finite and positive")
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            raise ValueError("Invalid candle OHLC bounds")
    params = dict(params or {})
    required = impl.min_history(params)
    if required < 1:
        raise ValueError("Strategy history must be positive")
    cash = initial_cash
    quantity, average, realized = 0, Decimal(0), Decimal(0)
    charges_total = Decimal(0)
    pending = []
    settlements = []
    settled = 0
    dp_days = set()
    prices, fills, curve = [], [], []
    peak, drawdown = initial_cash, Decimal(0)
    roundtrip_pnl, wins, trades, rejected = Decimal(0), 0, 0, 0
    # Per-trade P&L drives profit factor and average win/loss; exposure counts
    # the bars actually holding a position, so a return earned while mostly in
    # cash is not mistaken for a fully invested one.
    trade_pnls, exposure_bars = [], 0
    squared_off = 0
    previous_day = None
    for bar in candles:
        day = bar.ts.astimezone(IST).date()
        # Force-close an intraday position that the broker would have squared
        # off: at the cutoff on the same day, or -- if the data has no bar at
        # or after the cutoff -- on the first bar of the next day, using that
        # day's open. Holding MIS overnight models a product that does not
        # exist, and a backtest that does so reports gains no one could take.
        if product == ProductType.MIS and quantity > 0:
            crossed_cutoff = is_intraday_square_off_due(bar.ts)
            new_day = previous_day is not None and day != previous_day
            if crossed_cutoff or new_day:
                qty = quantity
                cost = compute_charges(
                    broker=broker,
                    side=OrderSide.SELL,
                    product=product,
                    quantity=qty,
                    price=bar.open,
                    exchange=exchange,
                    on=day,
                ).total
                notional = money(bar.open * qty)
                before = realized
                quantity, average, realized = apply_fill(
                    quantity, average, realized, -qty, bar.open
                )
                cash += notional - cost
                charges_total += cost
                roundtrip_pnl += realized - before - cost
                squared_off += 1
                trades += 1
                wins += roundtrip_pnl > 0
                trade_pnls.append(roundtrip_pnl)
                roundtrip_pnl = Decimal(0)
                fills.append(
                    dict(
                        ts=bar.ts.isoformat(),
                        side=OrderSide.SELL.value,
                        quantity=qty,
                        price=str(bar.open),
                        charges=str(cost),
                        square_off=True,
                    )
                )
        previous_day = day
        for due, qty in settlements[:]:
            if due <= day:
                settled += qty
                settlements.remove((due, qty))
        for signal in pending:
            side = (
                OrderSide.BUY
                if signal.signal_type in (SignalType.ENTRY_LONG, SignalType.EXIT_SHORT)
                else OrderSide.SELL
            )
            qty = signal.quantity
            # Keep round trips well-defined: no implicit reversals or pyramiding.
            valid = qty > 0 and signal.symbol == symbol
            if signal.signal_type == SignalType.ENTRY_LONG:
                valid &= quantity == 0
            elif signal.signal_type == SignalType.ENTRY_SHORT:
                valid = False  # Margin/collateral modeling is not implemented.
            elif signal.signal_type == SignalType.EXIT_LONG:
                valid &= quantity > 0 and qty <= quantity
            else:
                valid = False
            if product == ProductType.CNC and side == OrderSide.SELL:
                valid &= qty <= settled
            # No new intraday entry once the broker's square-off window has
            # opened: it would be closed again immediately, and a backtest that
            # allowed it would book a trade the market would never have given.
            if (
                product == ProductType.MIS
                and side == OrderSide.BUY
                and is_intraday_square_off_due(bar.ts)
            ):
                valid = False
            if not valid:
                rejected += 1
                continue
            first_sell = (
                product == ProductType.CNC and side == OrderSide.SELL and day not in dp_days
            )
            cost = compute_charges(
                broker=broker,
                side=side,
                product=product,
                quantity=qty,
                price=bar.open,
                exchange=exchange,
                is_first_sell_of_scrip_today=first_sell,
                on=day,
            ).total
            notional = money(bar.open * qty)
            if side == OrderSide.BUY and cash < notional + cost:
                rejected += 1
                continue
            due = (
                settlement_date(day)
                if product == ProductType.CNC and side == OrderSide.BUY
                else None
            )
            before = realized
            quantity, average, realized = apply_fill(
                quantity, average, realized, qty if side == OrderSide.BUY else -qty, bar.open
            )
            cash += (-notional if side == OrderSide.BUY else notional) - cost
            charges_total += cost
            roundtrip_pnl += realized - before - cost
            if due:
                settlements.append((due, qty))
            if product == ProductType.CNC and side == OrderSide.SELL:
                settled -= qty
                dp_days.add(day)
            if quantity == 0:
                trades += 1
                wins += roundtrip_pnl > 0
                trade_pnls.append(roundtrip_pnl)
                roundtrip_pnl = Decimal(0)
            fills.append(
                dict(
                    ts=bar.ts.isoformat(),
                    side=side.value,
                    quantity=qty,
                    price=str(bar.open),
                    charges=str(cost),
                )
            )
        if quantity:
            exposure_bars += 1
        equity = cash + quantity * bar.close
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
        curve.append(dict(ts=bar.ts.isoformat(), equity=str(equity)))
        prices.append(bar.close)
        pending = (
            impl.evaluate(
                StrategyContext(
                    symbol=symbol,
                    prices=prices[-required:],
                    position_quantity=quantity,
                    params=params,
                )
            )
            if len(prices) >= required
            else []
        )
    return dict(
        total_return=str((equity - initial_cash) / initial_cash),
        max_drawdown=str(drawdown),
        win_rate=wins / trades if trades else 0,
        trade_count=trades,
        fill_count=len(fills),
        total_charges=str(charges_total),
        final_equity=str(equity),
        open_quantity=quantity,
        rejected_signals=rejected,
        equity_curve=curve,
        fills=fills,
        # The final bar's decision has no next bar to fill against, so it is
        # dropped rather than executed. Reported so a reader comparing trade
        # counts against the strategy's own logic can see why one fewer fired,
        # instead of silently wondering.
        unfilled_final_signals=len(pending),
        # Intraday positions the broker would have force-closed. A strategy
        # relying on overnight holds shows up here rather than in the return.
        square_offs=squared_off,
        limitations=LIMITATIONS,
        # Return and win rate alone cannot distinguish a steady climb from a
        # violent one, nor a high win rate that loses money.
        # Recorded so a stored result says whose costs produced it. Rates and
        # plans change, and a result that cannot name its cost basis cannot be
        # compared against one run later.
        broker=str(getattr(broker, "value", broker)),
        risk_metrics=summarise_metrics(
            equity_curve=[Decimal(point["equity"]) for point in curve],
            interval=interval,
            trade_pnls=trade_pnls,
            total_return=(equity - initial_cash) / initial_cash,
            max_drawdown=drawdown,
            exposure_bars=exposure_bars,
        ),
    )
