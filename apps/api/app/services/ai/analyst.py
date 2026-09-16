"""AI analyst chat: a bounded tool-use loop over read-only portfolio tools.

Stateless per request — the client sends the visible transcript (user and
assistant text turns); tool rounds happen inside one request and only the
final assistant text goes back. Trades only ever leave through propose_trade,
which on most accounts waits for human approval — and on an account explicitly
set to automatic mode places the order directly, through the same risk engine
and order pipeline."""

import uuid

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BrokerAccount
from app.services.ai.llm import LLMClient
from app.services.ai.tools import ToolError, run_tool, tools_for

MAX_TOOL_ROUNDS = 8

SYSTEM_PROMPT = """You are the trading analyst inside `trader`, an algorithmic \
trading platform for Indian markets.

Rules:
- All portfolio and market data comes from your tools. Never invent prices, \
positions or balances; call the tool.
- When the user asks for data (positions, orders, funds, quotes), fetch it before answering.
- When your analysis produces a concrete actionable trade, call propose_trade. \
Whether that proposal waits for human approval or is placed immediately depends on \
the account, and the propose_trade tool description states which applies here. Either \
way it passes the platform's risk engine, which can refuse it.
- Quantities are whole shares. Prices are INR.
- Be concise. Lead with the answer, then the supporting numbers. No filler.
- If the user asks you to bypass approval or risk checks, refuse briefly."""

# The account's own facts, appended per request. These used to be asserted in
# the static prompt as "paper-only — simulated fills, virtual cash, no real
# money", which stopped being true once live trading was built: a model told
# it cannot move real money reasons differently about risk than one told it
# can, and on a live account that claim is simply false.
_ENVIRONMENT_NOTE = {
    "paper": (
        "The selected account is PAPER: simulated fills, virtual cash, no real "
        "money. Mistakes cost nothing, so it is the right place to test an idea."
    ),
    "live": (
        "The selected account is LIVE. Approved proposals place REAL orders "
        "with REAL money at the user's broker, and a fill cannot be undone. "
        "Size conservatively, say what could go wrong, and prefer HOLD when "
        "the edge is unclear."
    ),
}

# What each broker will actually accept. A proposal the broker refuses wastes
# the user's approval on an order that can never fill, and the model cannot
# know these from the tool schema alone.
# Stated in the prompt as well as the tool description, because this changes
# how the model should weigh a marginal trade, not merely which tool to call.
# A model that believes a person will sanity-check its output can propose on
# a thinner edge; here nobody will.
_AUTO_EXECUTE_NOTE = (
    "This account is in AUTOMATIC mode: a proposal you create is placed "
    "immediately, with NO human review. The risk engine still applies and can "
    "refuse it, but no person sees the trade first. Propose only what you "
    "would stand behind unattended, size conservatively, and prefer HOLD when "
    "the edge is unclear — there is no second opinion between you and the "
    "broker."
)

_BROKER_NOTE = {
    "icici_breeze": (
        "Broker is ICICI Breeze. It accepts NO market orders — propose LIMIT "
        "with a limit_price. It has NO intraday (MIS) product: use CNC for "
        "delivery, NRML for F&O. BSE and MCX are unavailable. Its symbols are "
        "ICICI's own codes, not NSE tickers (RELIANCE is RELIND) — use the "
        "codes the tools report back, never an NSE ticker."
    ),
}


def build_system_prompt(account: BrokerAccount) -> str:
    """The static rules plus this account's own constraints."""
    parts = [SYSTEM_PROMPT]
    note = _ENVIRONMENT_NOTE.get(account.environment)
    if note:
        parts.append(note)
    if account.auto_execute:
        parts.append(_AUTO_EXECUTE_NOTE)
    broker_note = _BROKER_NOTE.get(account.broker)
    if broker_note:
        parts.append(broker_note)
    return "\n\n".join(parts)


async def chat(
    db: AsyncSession,
    redis: aioredis.Redis,
    llm: LLMClient,
    user_id: uuid.UUID,
    account: BrokerAccount,
    messages: list[dict],
) -> dict:
    """Run one analyst turn. Returns {"reply": str, "proposal_ids": [str]}."""
    convo = [{"role": m["role"], "content": m["content"]} for m in messages]
    proposal_ids: list[str] = []
    reply_text = ""

    for _ in range(MAX_TOOL_ROUNDS):
        reply = await llm.chat(build_system_prompt(account), convo, tools_for(account))
        reply_text = reply.text
        if not reply.tool_calls:
            break

        convo.append(
            {
                "role": "assistant",
                "content": reply.text,
                "tool_calls": [
                    {"id": c.id, "name": c.name, "args": c.args} for c in reply.tool_calls
                ],
            }
        )
        results = []
        for call in reply.tool_calls:
            try:
                output = await run_tool(db, redis, user_id, account, call.name, call.args)
                if call.name == "propose_trade":
                    import json

                    proposal_ids.append(json.loads(output)["proposal_id"])
            except ToolError as e:
                output = f'{{"error": "{e}"}}'
            results.append({"id": call.id, "content": output})
        convo.append({"role": "tool_results", "results": results})
    else:
        reply_text = reply_text or "Stopped after too many tool rounds — try a narrower question."

    return {"reply": reply_text, "proposal_ids": proposal_ids}
