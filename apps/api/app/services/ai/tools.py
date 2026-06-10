"""Tools exposed to the AI analyst. All read-only against our own DB/redis —
except propose_trade, which records a PROPOSED row for human approval.
Nothing here talks to a broker or places an order."""

import json
import uuid
from decimal import Decimal, InvalidOperation

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AIProposal,
    BrokerAccount,
    FundsSnapshot,
    Order,
    Position,
    RiskRule,
    Strategy,
)
from app.domain.enums import AuditEventType, OrderSide, OrderType, ProductType
from app.engines.paper import market_sim
from app.services import audit

TOOLS: list[dict] = [
    {
        "name": "get_positions",
        "description": "Current open positions for the selected broker account.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_orders",
        "description": "The 20 most recent orders for the selected broker account.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_funds",
        "description": "Latest available-cash snapshot for the selected broker account.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_quotes",
        "description": "Current simulated prices and recent history for one or more symbols.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "NSE trading symbols, e.g. RELIANCE, TCS",
                }
            },
            "required": ["symbols"],
        },
    },
    {
        "name": "get_strategies",
        "description": "The user's configured strategies and their status.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_risk_rules",
        "description": "Active risk rules that orders must pass (loss caps, size limits…).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "propose_trade",
        "description": (
            "Call this when you want to suggest a trade. Creates a proposal that the "
            "user must approve before any order is placed — it never trades by itself. "
            "Use it whenever your analysis arrives at a concrete actionable trade."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "exchange": {"type": "string", "enum": ["NSE", "BSE"], "default": "NSE"},
                "side": {"type": "string", "enum": ["BUY", "SELL"]},
                "quantity": {"type": "integer", "minimum": 1},
                "order_type": {"type": "string", "enum": ["MARKET", "LIMIT"], "default": "MARKET"},
                "product": {"type": "string", "enum": ["MIS", "CNC"], "default": "MIS"},
                "limit_price": {"type": "number", "description": "Required for LIMIT orders"},
                "rationale": {
                    "type": "string",
                    "description": "Short explanation the user will read before approving",
                },
            },
            "required": ["symbol", "side", "quantity", "rationale"],
        },
    },
]


class ToolError(Exception):
    pass


def validate_proposal_args(args: dict) -> dict:
    """Normalize and validate propose_trade arguments. Raises ToolError."""
    symbol = str(args.get("symbol", "")).strip().upper()
    if not symbol:
        raise ToolError("symbol is required")
    try:
        side = OrderSide(str(args.get("side", "")).upper()).value
    except ValueError:
        raise ToolError(f"side must be BUY or SELL, got {args.get('side')!r}") from None
    try:
        quantity = int(args.get("quantity", 0))
    except (TypeError, ValueError):
        raise ToolError("quantity must be an integer") from None
    if quantity < 1:
        raise ToolError("quantity must be >= 1")
    try:
        order_type = OrderType(str(args.get("order_type", "MARKET")).upper()).value
    except ValueError:
        raise ToolError(f"unsupported order_type {args.get('order_type')!r}") from None
    try:
        product = ProductType(str(args.get("product", "MIS")).upper()).value
    except ValueError:
        raise ToolError(f"unsupported product {args.get('product')!r}") from None
    limit_price = None
    if args.get("limit_price") is not None:
        try:
            limit_price = Decimal(str(args["limit_price"]))
        except InvalidOperation:
            raise ToolError("limit_price must be a number") from None
    if order_type == OrderType.LIMIT.value and limit_price is None:
        raise ToolError("limit_price is required for LIMIT orders")
    rationale = str(args.get("rationale", "")).strip()
    if not rationale:
        raise ToolError("rationale is required")
    exchange = str(args.get("exchange", "NSE")).upper()
    if exchange not in ("NSE", "BSE"):
        raise ToolError("exchange must be NSE or BSE")
    return {
        "symbol": symbol,
        "exchange": exchange,
        "side": side,
        "quantity": quantity,
        "order_type": order_type,
        "product": product,
        "limit_price": limit_price,
        "rationale": rationale,
    }


def _num(value) -> str | None:
    return str(value) if value is not None else None


async def run_tool(
    db: AsyncSession,
    redis: aioredis.Redis,
    user_id: uuid.UUID,
    account: BrokerAccount,
    name: str,
    args: dict,
) -> str:
    """Execute one analyst tool and return a JSON string for the model."""
    if name == "get_positions":
        result = await db.execute(
            select(Position).where(Position.broker_account_id == account.id)
        )
        return json.dumps(
            [
                {
                    "symbol": p.symbol,
                    "exchange": p.exchange,
                    "product": p.product,
                    "quantity": p.quantity,
                    "average_price": _num(p.average_price),
                    "last_price": _num(p.last_price),
                    "realized_pnl": _num(p.realized_pnl),
                }
                for p in result.scalars()
            ]
        )

    if name == "get_orders":
        result = await db.execute(
            select(Order)
            .where(Order.broker_account_id == account.id)
            .order_by(Order.placed_at.desc())
            .limit(20)
        )
        return json.dumps(
            [
                {
                    "symbol": o.symbol,
                    "side": o.side,
                    "order_type": o.order_type,
                    "quantity": o.quantity,
                    "filled_quantity": o.filled_quantity,
                    "average_fill_price": _num(o.average_fill_price),
                    "status": o.status,
                    "placed_at": o.placed_at.isoformat(),
                }
                for o in result.scalars()
            ]
        )

    if name == "get_funds":
        result = await db.execute(
            select(FundsSnapshot)
            .where(FundsSnapshot.broker_account_id == account.id)
            .order_by(FundsSnapshot.ts.desc())
            .limit(1)
        )
        snap = result.scalar_one_or_none()
        if snap is None:
            return json.dumps({"available_cash": None, "note": "no funds snapshot yet"})
        return json.dumps({"available_cash": str(snap.available_cash), "as_of": snap.ts.isoformat()})

    if name == "get_quotes":
        symbols = [str(s).strip().upper() for s in args.get("symbols", []) if str(s).strip()]
        if not symbols:
            raise ToolError("symbols is required")
        out = {}
        for symbol in symbols[:10]:
            price = await market_sim.get_price(redis, symbol)
            history = await market_sim.get_history(redis, symbol, 30)
            out[symbol] = {"last_price": str(price), "recent": [str(p) for p in history]}
        return json.dumps(out)

    if name == "get_strategies":
        result = await db.execute(select(Strategy).where(Strategy.user_id == user_id))
        return json.dumps(
            [
                {"name": s.name, "kind": s.kind, "symbols": s.symbols, "status": s.status}
                for s in result.scalars()
            ]
        )

    if name == "get_risk_rules":
        result = await db.execute(
            select(RiskRule).where(RiskRule.user_id == user_id, RiskRule.enabled)
        )
        return json.dumps(
            [{"rule_type": r.rule_type, "params": r.params} for r in result.scalars()]
        )

    if name == "propose_trade":
        fields = validate_proposal_args(args)
        proposal = AIProposal(user_id=user_id, broker_account_id=account.id, **fields)
        db.add(proposal)
        await db.flush()
        await audit.emit(
            db,
            AuditEventType.AI_PROPOSAL,
            user_id=user_id,
            entity_type="ai_proposal",
            entity_id=proposal.id,
            payload={
                "action": "proposed",
                "symbol": fields["symbol"],
                "side": fields["side"],
                "quantity": fields["quantity"],
                "rationale": fields["rationale"],
            },
        )
        await db.commit()
        return json.dumps(
            {
                "proposal_id": str(proposal.id),
                "status": "PROPOSED",
                "note": "Awaiting human approval — do not assume it executed.",
            }
        )

    raise ToolError(f"Unknown tool: {name}")
