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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import BrokerAccount, Position, RiskRule, Strategy
from app.services.ai import analyst
from app.services.ai.llm import resolve_llm

logger = get_logger(__name__)

# How many symbols to put in front of the model. Kept small deliberately: each
# one costs tool calls against the broker's rate budget, and a list long enough
# to look like a screen would imply a breadth of data this platform does not
# have.
MAX_CANDIDATES = 8


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

You cannot see the wider market: these candidates are the only symbols this \
platform has data for. Do not speculate about stocks not on the list, and do \
not imply the list was screened for you."""


async def run_daily_research(db: AsyncSession, redis, account: BrokerAccount) -> dict:
    """One research pass for one account. Returns what it did, for the log."""
    symbols = await candidates(db, account)
    if not symbols:
        # Nothing to reason about. Said rather than silently doing nothing: an
        # account with no strategies has no subscribed symbols and therefore
        # no candles, which is a setup problem, not a quiet market.
        logger.info("ai_research_no_candidates", broker_account_id=str(account.id))
        return {"status": "no_candidates", "proposals": []}

    llm = await resolve_llm(db, account.user_id)
    if llm is None:
        logger.info("ai_research_no_provider", broker_account_id=str(account.id))
        return {"status": "no_provider", "proposals": []}

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

    prompt = PROMPT.format(symbols=", ".join(symbols), limits=_limits_note(rules))
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
