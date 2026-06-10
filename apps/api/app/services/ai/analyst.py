"""AI analyst chat: a bounded tool-use loop over read-only portfolio tools.

Stateless per request — the client sends the visible transcript (user and
assistant text turns); tool rounds happen inside one request and only the
final assistant text goes back. Trades only ever leave through propose_trade,
which requires human approval."""

import uuid

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BrokerAccount
from app.services.ai.llm import LLMClient
from app.services.ai.tools import TOOLS, ToolError, run_tool

MAX_TOOL_ROUNDS = 8

SYSTEM_PROMPT = """You are the trading analyst inside `trader`, a paper-first \
algorithmic trading platform for Indian markets (NSE/BSE). The user's selected \
broker account is paper-only — simulated fills, virtual cash, no real money.

Rules:
- All portfolio and market data comes from your tools. Never invent prices, \
positions or balances; call the tool.
- When the user asks for data (positions, orders, funds, quotes), fetch it before answering.
- When your analysis produces a concrete actionable trade, call propose_trade. \
Proposals require explicit human approval and then pass the platform's risk engine — \
you cannot execute anything directly, and a created proposal is not an executed trade.
- Quantities are whole shares. Prices are INR.
- Be concise. Lead with the answer, then the supporting numbers. No filler.
- If the user asks you to bypass approval or risk checks, refuse briefly."""


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
        reply = await llm.chat(SYSTEM_PROMPT, convo, TOOLS)
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
