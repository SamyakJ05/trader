"""Translate NSE tickers into the stock codes ICICI Breeze actually accepts.

Breeze does not use NSE trading symbols. RELIANCE is RELIND, and a strategy
naming the NSE ticker is refused at creation because it would otherwise find
no candles, emit no signals and look merely quiet.

This resolves a list of NSE tickers to Breeze's own codes using the synced
instrument master, by two routes:

  1. ISIN — the same identifier at every venue, so a Zerodha or NSE row and a
     Breeze row carrying the same ISIN name the same security. Exact, when
     another broker's master is present to bridge through.
  2. The name field — Breeze's master stores "RELIANCE INDUSTRIES (RELIANCE)",
     with the NSE ticker in parentheses. Used when no second master exists to
     bridge through, which is the common case on a Breeze-only instance.

Run it read-only first; pass --sync to refresh the master when a ticker is
missing because the master is stale rather than because the code differs.

    python -m scripts.breeze_codes ASHOKLEY TATAELXSI PIDILITIND
    python -m scripts.breeze_codes --sync ASHOKLEY TATAELXSI
"""

import argparse
import asyncio
import re
import sys

from sqlalchemy import func, or_, select

from app.db.models import BrokerAccount, MarketInstrument
from app.db.session import async_session_factory
from app.services.instruments import sync_instruments

BROKER = "icici_breeze"


async def _breeze_account(db) -> BrokerAccount | None:
    return (
        await db.execute(
            select(BrokerAccount)
            .where(BrokerAccount.broker == BROKER)
            .order_by(BrokerAccount.created_at)
        )
    ).scalars().first()


async def _master_size(db, exchange: str) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(MarketInstrument)
            .where(
                MarketInstrument.broker == BROKER,
                MarketInstrument.exchange == exchange,
            )
        )
    ).scalar_one()


async def resolve(db, ticker: str, exchange: str = "NSE") -> list[tuple[str, str, str]]:
    """Breeze codes matching one NSE ticker, as (code, name, how_it_matched)."""
    ticker = ticker.strip().upper()

    # Exact hit: already a Breeze code.
    exact = (
        await db.execute(
            select(MarketInstrument.symbol, MarketInstrument.name).where(
                MarketInstrument.broker == BROKER,
                MarketInstrument.exchange == exchange,
                MarketInstrument.symbol == ticker,
            )
        )
    ).first()
    if exact:
        return [(exact[0], exact[1] or "", "already a Breeze code")]

    # Route 1 — ISIN, via any other broker's row for this ticker.
    isin = (
        await db.execute(
            select(MarketInstrument.isin).where(
                MarketInstrument.broker != BROKER,
                MarketInstrument.exchange == exchange,
                MarketInstrument.symbol == ticker,
                MarketInstrument.isin.isnot(None),
            )
        )
    ).scalars().first()
    if isin:
        rows = (
            await db.execute(
                select(MarketInstrument.symbol, MarketInstrument.name).where(
                    MarketInstrument.broker == BROKER,
                    MarketInstrument.exchange == exchange,
                    MarketInstrument.isin == isin,
                )
            )
        ).all()
        if rows:
            return [(r[0], r[1] or "", f"ISIN {isin}") for r in rows]

    # Route 2 — the name field. Breeze writes "... (NSETICKER)", so the
    # parenthesised ticker is matched first and exactly; a bare substring
    # search would make ASHOKLEY match any name merely containing it.
    rows = (
        await db.execute(
            select(MarketInstrument.symbol, MarketInstrument.name).where(
                MarketInstrument.broker == BROKER,
                MarketInstrument.exchange == exchange,
                or_(
                    MarketInstrument.name.ilike(f"%({ticker})"),
                    MarketInstrument.name.ilike(f"%({ticker})%"),
                ),
            )
        )
    ).all()
    if rows:
        return [(r[0], r[1] or "", "name contains (TICKER)") for r in rows]

    # Last resort, reported as a guess rather than an answer: the leading
    # alphabetic run of the ticker, which is how several Breeze codes are
    # abbreviated (ASHOKLEY -> ASHLEY). Never auto-applied.
    stem = re.match(r"^[A-Z]+", ticker)
    if stem and len(stem.group()) >= 4:
        rows = (
            await db.execute(
                select(MarketInstrument.symbol, MarketInstrument.name)
                .where(
                    MarketInstrument.broker == BROKER,
                    MarketInstrument.exchange == exchange,
                    MarketInstrument.name.ilike(f"{stem.group()[:4]}%"),
                )
                .limit(5)
            )
        ).all()
        if rows:
            return [(r[0], r[1] or "", "GUESS — verify before use") for r in rows]

    return []


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="+", help="NSE tickers, e.g. ASHOKLEY")
    parser.add_argument("--exchange", default="NSE")
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Refresh the Breeze instrument master first (one broker call)",
    )
    args = parser.parse_args()

    async with async_session_factory() as db:
        account = await _breeze_account(db)
        if account is None:
            print("No icici_breeze account found. Connect one first.")
            return 1

        size = await _master_size(db, args.exchange)
        print(f"Breeze {args.exchange} master: {size} rows")

        if args.sync or size == 0:
            if size == 0:
                print("Master is empty — syncing (an empty master validates nothing).")
            print("Syncing instrument master...")
            written = await sync_instruments(db, account, exchange=args.exchange)
            await db.commit()
            print(f"Wrote {written} rows.\n")

        unresolved = []
        for ticker in args.tickers:
            matches = await resolve(db, ticker, args.exchange)
            if not matches:
                unresolved.append(ticker)
                print(f"{ticker:<14} -> NOT FOUND")
                continue
            code, name, how = matches[0]
            print(f"{ticker:<14} -> {code:<14} {name}   [{how}]")
            for code, name, how in matches[1:]:
                print(f"{'':<14}    {code:<14} {name}   [{how}]")

        if unresolved:
            print(
                "\nUnresolved: "
                + ", ".join(unresolved)
                + "\nNot every NSE stock is tradable on Breeze, and BSE/MCX are not "
                "available at all. Check the code on ICICI Direct's own site before "
                "assuming the instrument is missing."
            )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
