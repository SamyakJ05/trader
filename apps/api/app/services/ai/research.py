"""A daily research pass: the analyst reviews candidates and proposes trades.

Runs once before the open. It hands the analyst a candidate list and the
account's own risk limits, and lets it use the tools it already has -- news,
indicators, quotes, past proposals and their outcomes -- to decide whether
anything is worth doing today. What comes back is proposals, which a human
approves; on an account with auto_execute on it is the platform's ordinary
auto-execute path, with the same risk engine and the same daily cap.

WHAT THIS IS NOT. It is not a screen of the whole market. The analyst can only
compute indicators on symbols this platform has candles for, and candles exist
only for what the tick stream has subscribed to, which is what strategies name.
So the candidate list is the universe, and it is small and explicit rather than
discovered. Widening it means feeding the platform market data it does not
currently have -- an end-of-day bhavcopy import would do it, and until that
exists a promise to "find today's best stocks" would be a promise about data
nobody has.

It is also not a claim that trading daily is profitable. The prompt says so
plainly, because a model asked for a daily trade will find a reason for one,
and the honest answer on most days is that nothing is worth the charges.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import (
    BrokerAccount,
    MarketInstrument,
    Position,
    RiskRule,
    Strategy,
)
from app.services import bhavcopy, quotes
from app.services.ai import analyst
from app.services.ai.llm import AIConfigurationError, resolve_llm

logger = get_logger(__name__)

# How many symbols to put in front of the model. Kept small deliberately: each
# one costs tool calls against the broker's rate budget, and a list long enough
# to look like a screen would imply a breadth of data this platform does not
# have.
MAX_CANDIDATES = 8

# How many instruments to price before giving up on finding affordable ones.
# Breeze allows 100 calls a minute and 5,000 a day; this pass runs once, so a
# few dozen is comfortably inside both and still enough to find candidates.
# The cap exists because the master holds ~5,900 NSE rows and pricing all of
# them would spend the day's quota on a screen.
MAX_PRICED = 40

# A candidate has to be buyable in a size that can be exited in parts. One
# share of a scrip that eats the whole per-order limit is an all-or-nothing
# position: it cannot be scaled out of, and a stop becomes a full exit.
MIN_AFFORDABLE_SHARES = 5


async def candidates(db: AsyncSession, account: BrokerAccount) -> list[str]:
    """Symbols the analyst can actually reason about, in the broker's codes.

    Strategy symbols first, because those are the ones the tick stream
    subscribes to and therefore the ones with candles. Anything held is added
    regardless -- a position the analyst cannot see is one it cannot suggest
    closing, which is the more useful half of a daily review.
    """
    symbols: list[str] = []

    rows = (
        await db.execute(
            select(Strategy.symbols).where(
                Strategy.broker_account_id == account.id,
                Strategy.status.in_(["RUNNING", "DRAFT"]),
            )
        )
    ).scalars()
    for row in rows:
        for symbol in row or []:
            if symbol not in symbols:
                symbols.append(symbol)

    # Open positions, whether or not a strategy still names them. A holding
    # the analyst cannot see is one it cannot suggest closing, and on most
    # days reviewing what is already held is the more useful half of this.
    held = (
        await db.execute(
            select(Position.symbol).where(
                Position.broker_account_id == account.id,
                Position.quantity != 0,
            )
        )
    ).scalars()
    for symbol in held:
        if symbol and symbol not in symbols:
            symbols.append(symbol)

    return symbols[:MAX_CANDIDATES]


async def affordable_candidates(
    db: AsyncSession, redis, account: BrokerAccount, *, budget: Decimal
) -> list[tuple[str, Decimal]]:
    """Equities this account can buy several shares of, with their prices.

    The instrument master carries no prices -- it is reference data -- so
    affordability cannot be read from it and has to be asked of the broker.
    That is why this is capped and why it runs once a day rather than per
    request.

    `budget` is the per-order ceiling, not the daily one: what matters is
    whether a single order can take a position of a useful size. A scrip
    priced so that one share spends the whole order limit is excluded, because
    a position that cannot be scaled out of is one where every exit is a full
    exit.

    Returns (symbol, price) so the caller can size without asking again.
    """
    rows = list(
        (
            await db.execute(
                select(MarketInstrument.symbol)
                .where(
                    MarketInstrument.broker == account.broker,
                    MarketInstrument.exchange == "NSE",
                    MarketInstrument.instrument_type == "EQ",
                )
                .order_by(MarketInstrument.symbol)
                .limit(MAX_PRICED)
            )
        ).scalars()
    )

    found: list[tuple[str, Decimal]] = []
    for symbol in rows:
        if len(found) >= MAX_CANDIDATES:
            break
        price = await quotes.live_price(
            db, redis, symbol=symbol, exchange="NSE", account=account
        )
        if price is None or price <= 0:
            continue
        if price * MIN_AFFORDABLE_SHARES <= budget:
            found.append((symbol, price))
    return found


def per_order_budget(rules: list[RiskRule]) -> Decimal:
    """The largest single order the risk engine would allow.

    Read from the rules rather than assumed, so the screen and the limits
    cannot disagree about what is affordable.
    """
    for rule in rules:
        if rule.rule_type == "MAX_ORDER_NOTIONAL" and rule.enabled:
            return Decimal(str((rule.params or {}).get("max_notional", 0)))
    return Decimal(0)


def _limits_note(rules: list[RiskRule]) -> str:
    """The account's real limits, in the prompt.

    A model that does not know the ceilings proposes through them, and the
    risk engine then refuses the order -- which spends a broker call and
    teaches the model nothing, since it never sees the rejection.
    """
    parts = []
    for rule in rules:
        if not rule.enabled:
            continue
        params = rule.params or {}
        if rule.rule_type == "MAX_DAILY_TURNOVER":
            parts.append(f"at most Rs {params.get('max_turnover')} bought in total today")
        elif rule.rule_type == "MAX_ORDER_NOTIONAL":
            parts.append(f"no single order over Rs {params.get('max_notional')}")
        elif rule.rule_type == "MAX_TOTAL_EXPOSURE":
            parts.append(f"no more than Rs {params.get('max_exposure')} held at once")
        elif rule.rule_type == "MAX_DAILY_LOSS":
            parts.append(
                f"trading halts for the day at Rs {params.get('max_loss')} of "
                "realised loss"
            )
        elif rule.rule_type == "MAX_AUTO_TRADES_PER_DAY":
            parts.append(f"at most {params.get('max_auto_trades')} automatic trades a day")
    return "; ".join(parts) or "no limits are configured, which is itself worth saying"


PROMPT = """Review today's candidates for this account and decide whether any \
trade is worth making.

Candidates (these are the broker's own stock codes — use them exactly): {symbols}

The account's risk limits, which the platform enforces regardless of what you \
propose: {limits}

For each candidate worth considering, check the indicators and any news before \
forming a view. Then either call propose_trade, or say plainly that nothing is \
worth doing.

Doing nothing is the right answer on most days. Charges are paid per trade and \
a marginal edge does not survive them, so propose only what you would defend \
on its own merits — not because a review was scheduled. If you do propose, size \
it well inside the limits above rather than at them, and say what would make \
you wrong.

Position size matters as much as the pick. A cheaper share lets the same rupees \
buy a position that can be scaled out of; one share of an expensive scrip is an \
all-or-nothing trade where every exit is a full exit. Prefer a size you could \
halve.

These candidates are the symbols this platform has data for plus equities \
priced inside the per-order limit — they are filtered for affordability, NOT \
ranked for quality, and the wider market is not visible to you. Do not \
speculate about stocks not on the list, and do not imply it was screened for \
merit."""


async def run_daily_research(db: AsyncSession, redis, account: BrokerAccount) -> dict:
    """One research pass for one account. Returns what it did, for the log."""
    rules = list(
        (
            await db.execute(
                select(RiskRule).where(
                    RiskRule.user_id == account.user_id,
                    RiskRule.environment == account.environment,
                )
            )
        ).scalars()
    )

    symbols = await candidates(db, account)

    # Widen to what this account can actually afford. Strategy symbols are
    # whatever someone configured, and a scrip priced beyond the per-order
    # limit cannot be traded at all -- proposing it wastes the pass. Priced
    # candidates are appended with their prices so the model can size against
    # a real number rather than guess.
    budget = per_order_budget(rules)
    priced: list[tuple[str, Decimal]] = []
    if budget > 0:
        try:
            # The stored bhavcopy first: one query over every NSE equity,
            # ranked by traded value, against 40 broker calls that could only
            # filter. Falls back to pricing through the broker when no
            # bhavcopy has been imported yet -- a fresh deployment has none
            # until the nightly job has run once.
            screened = await bhavcopy.screen(
                db,
                broker=account.broker,
                max_price=budget,
                min_shares=MIN_AFFORDABLE_SHARES,
                limit=MAX_CANDIDATES,
            )
            priced = [(row["symbol"], row["close"]) for row in screened]
            if not priced:
                priced = await affordable_candidates(db, redis, account, budget=budget)
        except Exception as exc:
            # A screen is a nicety; the configured symbols still work without
            # it. Broker quota or a dead session must not cost the whole pass.
            logger.warning("ai_research_screen_failed", error=str(exc))
    for symbol, _price in priced:
        if symbol not in symbols:
            symbols.append(symbol)
    symbols = symbols[:MAX_CANDIDATES]

    if not symbols:
        # Nothing to reason about. Said rather than silently doing nothing: an
        # account with no strategies has no subscribed symbols and therefore
        # no candles, which is a setup problem, not a quiet market.
        logger.info("ai_research_no_candidates", broker_account_id=str(account.id))
        return {"status": "no_candidates", "proposals": []}

    try:
        llm = await resolve_llm(db, account.user_id)
    except AIConfigurationError as exc:
        # Distinct from having no provider: the person configured one and it
        # cannot be used, which is a thing to fix rather than a thing to set up.
        logger.warning(
            "ai_research_bad_provider",
            broker_account_id=str(account.id),
            detail=str(exc),
        )
        return {"status": "bad_provider", "proposals": []}
    if llm is None:
        logger.info("ai_research_no_provider", broker_account_id=str(account.id))
        return {"status": "no_provider", "proposals": []}

    price_note = (
        " Prices seen just now: "
        + ", ".join(f"{sym} at Rs {px}" for sym, px in priced)
        if priced
        else ""
    )
    prompt = PROMPT.format(
        symbols=", ".join(symbols) + price_note, limits=_limits_note(rules)
    )
    result = await analyst.chat(
        db,
        redis,
        llm,
        account.user_id,
        account,
        [{"role": "user", "content": prompt}],
    )
    logger.info(
        "ai_research_complete",
        broker_account_id=str(account.id),
        candidates=len(symbols),
        proposals=len(result.get("proposal_ids", [])),
    )
    return {"status": "ok", "proposals": result.get("proposal_ids", [])}


async def research_all_accounts(db: AsyncSession, redis) -> list[dict]:
    """Every account with AI configured. One failure must not stop the rest."""
    accounts = list(
        (await db.execute(select(BrokerAccount))).scalars()
    )
    results = []
    for account in accounts:
        try:
            results.append(await run_daily_research(db, redis, account))
        except Exception as exc:
            # Research is advisory. A provider outage or a rate limit must not
            # mark the worker unhealthy or interrupt the ticks that move money.
            logger.warning(
                "ai_research_failed",
                broker_account_id=str(account.id),
                error=str(exc),
            )
            results.append({"status": "failed", "proposals": []})
    return results
