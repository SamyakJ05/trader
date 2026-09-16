"""Tools exposed to the AI analyst. All read-only against our own DB/redis —
except propose_trade, which records a PROPOSED row for human approval.
Nothing here talks to a broker or places an order."""

import json
import uuid
from datetime import date
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
from app.domain.enums import AuditEventType, Broker, OrderSide, OrderType, ProductType
from app.engines.paper import market_sim
from app.services import audit, quotes

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
        "description": (
            "Current prices and recent history for one or more symbols. On a "
            "live account these are real market quotes, and a symbol with no "
            "recent quote reports none rather than an estimate."
        ),
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
                "exchange": {
                    "type": "string",
                    "enum": ["NSE", "BSE", "NFO"],
                    "default": "NSE",
                    "description": "NFO for futures and options.",
                },
                "side": {"type": "string", "enum": ["BUY", "SELL"]},
                "quantity": {"type": "integer", "minimum": 1},
                "order_type": {"type": "string", "enum": ["MARKET", "LIMIT"], "default": "MARKET"},
                "product": {"type": "string", "enum": ["MIS", "CNC"], "default": "MIS"},
                "limit_price": {"type": "number", "description": "Required for LIMIT orders"},
                "rationale": {
                    "type": "string",
                    "description": "Short explanation the user will read before approving",
                },
                "expiry": {
                    "type": "string",
                    "description": (
                        "Contract expiry as YYYY-MM-DD. Required for NFO: a "
                        "symbol alone does not name a contract, since the same "
                        "underlying has many expiries and strikes."
                    ),
                },
                "strike": {
                    "type": "number",
                    "description": "Option strike. Omit for a futures contract.",
                },
                "right": {
                    "type": "string",
                    "enum": ["call", "put", "others"],
                    "description": "'others' for a future.",
                },
            },
            "required": ["symbol", "side", "quantity", "rationale"],
        },
    },
]


class ToolError(Exception):
    pass


def validate_proposal_args(args: dict, *, broker: str | None = None) -> dict:
    """Normalize and validate propose_trade arguments. Raises ToolError.

    `broker` narrows the accepted values to what that broker can actually
    accept. Without it the model was free to propose MARKET/MIS/BSE on a
    Breeze account, every one of which Breeze refuses -- so an approved
    proposal became a guaranteed rejection, discovered only after the user
    had approved a real trade. Refusing here instead lets the model correct
    itself: the tool error goes back into its context.
    """
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
    if exchange not in ("NSE", "BSE", "NFO"):
        raise ToolError("exchange must be NSE, BSE or NFO")

    # A derivatives proposal has to name its contract. Without this the
    # approval would place a cash order in the underlying instead -- a
    # different position, at a different price, with different margin.
    expiry = strike = option_right = None
    if exchange == "NFO":
        raw_expiry = str(args.get("expiry", "")).strip()
        if not raw_expiry:
            raise ToolError(
                "expiry is required for NFO (YYYY-MM-DD): a symbol alone does "
                "not name a contract."
            )
        try:
            expiry = date.fromisoformat(raw_expiry)
        except ValueError:
            raise ToolError(f"expiry must be YYYY-MM-DD, got {raw_expiry!r}") from None
        raw_right = str(args.get("right", "")).strip().lower()
        if args.get("strike") is not None:
            try:
                strike = Decimal(str(args["strike"]))
            except InvalidOperation:
                raise ToolError("strike must be a number") from None
            if not raw_right:
                raise ToolError("right is required with a strike: call or put")
        if raw_right and raw_right not in ("call", "put", "others"):
            raise ToolError(f"right must be call, put or others, got {raw_right!r}")
        if raw_right in ("call", "put") and strike is None:
            raise ToolError("an option needs a strike")
        option_right = (raw_right or "others").upper()

    if broker == Broker.ICICI_BREEZE.value:
        # Each of these is a refusal at the adapter, and the model cannot know
        # them from the schema alone. Named explicitly so the error tells it
        # what to do instead rather than only what is wrong.
        if exchange == "BSE":
            raise ToolError(
                "ICICI Breeze does not offer BSE — their API documents BSE and "
                "MCX as unavailable. Use NSE."
            )
        if product == ProductType.MIS.value:
            raise ToolError(
                "ICICI Breeze has no MIS (intraday) product. Use CNC for "
                "delivery or NRML for F&O."
            )
        if order_type == OrderType.MARKET.value:
            raise ToolError(
                "ICICI Breeze accepts no market orders. Propose a LIMIT order "
                "with a limit_price, priced to cross the spread if you want it "
                "to fill immediately."
            )
    return {
        "symbol": symbol,
        "exchange": exchange,
        "side": side,
        "quantity": quantity,
        "order_type": order_type,
        "product": product,
        "limit_price": limit_price,
        "rationale": rationale,
        "expiry": expiry,
        "strike": strike,
        "option_right": option_right,
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
        result = await db.execute(select(Position).where(Position.broker_account_id == account.id))
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
        if account.environment == "paper":
            from app.engines.paper.ledger import get_cash

            return json.dumps(
                {"available_cash": str(await get_cash(db, account.id)), "source": "cash_ledger"}
            )
        result = await db.execute(
            select(FundsSnapshot)
            .where(FundsSnapshot.broker_account_id == account.id)
            .order_by(FundsSnapshot.ts.desc())
            .limit(1)
        )
        snap = result.scalar_one_or_none()
        if snap is None:
            return json.dumps({"available_cash": None, "note": "no funds snapshot yet"})
        return json.dumps(
            {"available_cash": str(snap.available_cash), "as_of": snap.ts.isoformat()}
        )

    if name == "get_quotes":
        symbols = [str(s).strip().upper() for s in args.get("symbols", []) if str(s).strip()]
        if not symbols:
            raise ToolError("symbols is required")
        out = {}
        is_live = account.environment != "paper"
        for symbol in symbols[:10]:
            if is_live:
                # The simulator invents a price for any symbol it has not
                # seen. Handing that to an assistant on a live account would
                # have it reason about, and recommend trades from, a number
                # with no relationship to the market — and the user would have
                # no way to tell. Say there is no quote instead.
                price = await quotes.live_price(db, redis, symbol=symbol)
                out[symbol] = (
                    {"last_price": str(price), "source": "market"}
                    if price is not None
                    else {
                        "last_price": None,
                        "note": (
                            "No live quote available. Do not estimate a price "
                            "or recommend a trade in this symbol."
                        ),
                    }
                )
                continue
            price = await market_sim.get_price(redis, symbol)
            history = await market_sim.get_history(redis, symbol, 30)
            out[symbol] = {
                "last_price": str(price),
                "recent": [str(p) for p in history],
                "source": "simulator",
            }
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
        fields = validate_proposal_args(args, broker=account.broker)
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
