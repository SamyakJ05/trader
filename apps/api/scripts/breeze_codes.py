"""Translate NSE tickers into the stock codes ICICI Breeze actually accepts.

Breeze does not use NSE trading symbols. RELIANCE is RELIND, and a strategy
naming the NSE ticker is refused at creation because it would otherwise find
no candles, emit no signals and look merely quiet.

This resolves a list of NSE tickers to Breeze's own codes using the synced
instrument master, in descending order of confidence:

  1. ISIN — the same identifier at every venue, so another broker's row and a
     Breeze row carrying the same ISIN name the same security. Exact, and
     available whenever a second broker's master has been synced.
  2. The name field, for a master that carries the ticker in it.
  3. A one-character near-miss on the code itself, reported as a likely typo:
     RELIIND is not a ticker needing translation, it is RELIND misspelt.
  4. The company name's leading letters, for a Breeze-only instance where
     nothing else links INFY to "INFOSYS LTD". Offered as candidates to
     confirm, never as an answer.

Only 1 and 2 are identifications. Anything below them is labelled in the
output as something to verify, because a plausible-looking wrong stock code is
worse than no answer when the next step is an order.

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


def _one_edit_apart(a: str, b: str) -> bool:
    """True when a and b differ by a single insertion, deletion or swap.

    Deliberately not a full edit-distance: one edit is the distance between a
    code and a typo of it (RELIND / RELIIND), while two already reaches
    genuinely different instruments and would turn a typo hint into a wrong
    suggestion next to an order form.
    """
    a, b = a.upper(), b.upper()
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    i = j = 0
    skipped = False
    while i < len(shorter) and j < len(longer):
        if shorter[i] != longer[j]:
            if skipped:
                return False
            skipped = True
            j += 1
            continue
        i += 1
        j += 1
    return True


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

    # Route 2 — the name field.
    #
    # What is in `name` depends on which master the row came from. Breeze's
    # NSE equity file has NO Symbol column at all (header: Token, ShortName,
    # Series, CompanyName, ticksize, Lotsize, ..., ISINCode), so `name` holds
    # the company name alone -- "RELIANCE INDUSTRIES", never "(RELIANCE)".
    # Matching a parenthesised ticker therefore finds nothing on NSE equities,
    # and the company name is all there is to search.
    #
    # Both shapes are tried: the parenthesised form for any master that does
    # carry it, then the company name. The company-name match is anchored to
    # a word boundary rather than a bare substring, so INFY does not match
    # every name that merely contains those letters.
    rows = (
        await db.execute(
            select(MarketInstrument.symbol, MarketInstrument.name).where(
                MarketInstrument.broker == BROKER,
                MarketInstrument.exchange == exchange,
                or_(
                    MarketInstrument.name.ilike(f"%({ticker})%"),
                    MarketInstrument.name.ilike(f"{ticker} %"),
                    MarketInstrument.name.ilike(f"% {ticker} %"),
                    MarketInstrument.name == ticker,
                ),
            )
        )
    ).all()
    if rows:
        return [(r[0], r[1] or "", "matched on name") for r in rows]

    # Near-miss on the code itself, before any guessing: RELIIND is not an NSE
    # ticker needing translation, it is RELIND misspelt, and saying so is a
    # better answer than "not found" -- which reads as "Breeze does not offer
    # Reliance" and sends someone hunting for a code they already had.
    close = (
        await db.execute(
            select(MarketInstrument.symbol, MarketInstrument.name).where(
                MarketInstrument.broker == BROKER,
                MarketInstrument.exchange == exchange,
            )
        )
    ).all()
    near = [
        (code, name or "")
        for code, name in close
        if code and _one_edit_apart(code, ticker)
    ]
    if near:
        return [
            (code, name, f"TYPO? you wrote {ticker}, this is one character away")
            for code, name in near[:5]
        ]

    # Route 3 — the company name, for a Breeze-only instance.
    #
    # Without a second broker's master there is no ISIN to bridge through, and
    # Breeze's NSE file carries no ticker, so INFY has nothing to match: the
    # row says "INFOSYS LTD". The ticker's leading letters are the only link
    # left, and 4 characters is the shortest prefix that is not noise -- INFY
    # finds INFOSYS, while 3 would let any three letters match half the file.
    #
    # Offered as candidates to confirm, never as the answer, because a prefix
    # is a resemblance and not an identification.
    if len(ticker) >= 4:
        rows = (
            await db.execute(
                select(MarketInstrument.symbol, MarketInstrument.name)
                .where(
                    MarketInstrument.broker == BROKER,
                    MarketInstrument.exchange == exchange,
                    MarketInstrument.name.ilike(f"{ticker[:4]}%"),
                )
                .limit(5)
            )
        ).all()
        if rows:
            return [
                (r[0], r[1] or "", "name starts with your ticker — CONFIRM before use")
                for r in rows
            ]

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
