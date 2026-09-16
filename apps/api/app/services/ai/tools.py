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
    HoldingsSnapshot,
    Order,
    Position,
    RiskRule,
    Strategy,
)
from app.domain.enums import AuditEventType, Broker, OrderSide, OrderType, ProductType
from app.engines.market.candles import history as candle_history
from app.engines.paper import market_sim
from app.engines.strategy.base import Bar
from app.engines.strategy.indicators import atr, bollinger, macd, rsi, sma
from app.services import audit, quotes
from app.services import news as news_service
from app.workers.tick_stream import LIVE_SOURCE, LIVE_SOURCES

TOOLS: list[dict] = [
    {
        "name": "get_positions",
        "description": "Current open positions for the selected broker account.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_holdings",
        "description": (
            "Shares already settled in the demat account: stock the user owns "
            "outright, separate from intraday positions. get_positions does "
            "NOT include these. An account can hold stock worth lakhs and "
            "still report zero positions, so check both before concluding "
            "the user holds nothing. Quantity is what is sellable today; "
            "total_quantity includes pledged and blocked stock. ICICI Breeze "
            "reports no cost basis, so average_price is often null."
        ),
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
        "name": "get_indicators",
        "description": (
            "Technical indicators for a symbol, computed from stored candles: "
            "RSI, MACD, Bollinger bands, ATR, and moving averages. Prefer this "
            "over reasoning about raw prices -- these are the same "
            "implementations the rule-based strategies trade on, so a reading "
            "quoted here matches what they would act on. A value is null when "
            "there is not enough history for it, which means unknown, NOT "
            "neutral: do not treat a null RSI as 50."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "NSE trading symbol, e.g. RELIANCE",
                },
                "interval": {
                    "type": "string",
                    "description": "Candle interval, default 1m",
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_news",
        "description": (
            "Recent corporate announcements and financial headlines, newest "
            "first. Pass a symbol for items about that company plus "
            "market-wide ones; omit it for market-wide only. These are "
            "official filings and published press feeds, NOT social media. "
            "Treat them as context for a recommendation, never as the sole "
            "reason for one, and cite the title you relied on. An empty "
            "result means nothing was FETCHED -- which can mean a quiet news "
            "day or a feed outage -- so it is never evidence that a holding "
            "is safe."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "NSE trading symbol, e.g. RELIANCE. Optional.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum items, default 20, capped at 50.",
                },
            },
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

    if name == "get_holdings":
        # A JSONB snapshot written by the account sync, not a live call: the
        # analyst must not trigger broker traffic, and the sync is the single
        # place that talks to the broker.
        result = await db.execute(
            select(HoldingsSnapshot)
            .where(HoldingsSnapshot.broker_account_id == account.id)
            .order_by(HoldingsSnapshot.ts.desc())
            .limit(1)
        )
        snap = result.scalar_one_or_none()
        if snap is None:
            return json.dumps(
                {
                    "holdings": [],
                    "as_of": None,
                    "note": (
                        "No holdings snapshot yet. This means the account has "
                        "not been synced, NOT that the user owns no stock."
                    ),
                }
            )
        return json.dumps(
            {"holdings": snap.holdings, "as_of": snap.ts.isoformat()}, default=str
        )

    if name == "get_indicators":
        symbol = str(args.get("symbol", "")).strip().upper()
        if not symbol:
            raise ToolError("symbol is required")
        interval = str(args.get("interval") or "1m")
        is_live = account.environment != "paper"
        source = (
            LIVE_SOURCES.get(account.broker, LIVE_SOURCE) if is_live else "simulator"
        )
        # Enough for the longest indicator here (MACD needs slow + signal),
        # with headroom so a reading is smoothed rather than seed-only.
        candles = await candle_history(db, symbol, "NSE", interval, source, 120)
        closes = [c.close for c in candles]
        bars = [
            Bar(open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume)
            for c in candles
        ]

        macd_values = macd(closes)
        bands = bollinger(closes)
        # str() not float(): these are Decimals, and rendering them through a
        # binary float would reintroduce the representation error the
        # indicator module exists to avoid.
        return json.dumps(
            {
                "symbol": symbol,
                "interval": interval,
                "candles_available": len(closes),
                "source": source,
                "last_close": str(closes[-1]) if closes else None,
                "rsi_14": str(rsi(closes)) if rsi(closes) is not None else None,
                "macd": (
                    {
                        "line": str(macd_values[0]),
                        "signal": str(macd_values[1]),
                        "histogram": str(macd_values[2]),
                    }
                    if macd_values
                    else None
                ),
                "bollinger": (
                    {
                        "lower": str(bands[0]),
                        "middle": str(bands[1]),
                        "upper": str(bands[2]),
                    }
                    if bands
                    else None
                ),
                "atr_14": str(atr(bars)) if atr(bars) is not None else None,
                "sma_20": str(sma(closes, 20)) if sma(closes, 20) is not None else None,
                "sma_50": str(sma(closes, 50)) if sma(closes, 50) is not None else None,
                "note": (
                    "A null value means insufficient history, not a neutral "
                    "reading."
                ),
            }
        )

    if name == "get_news":
        symbol = str(args.get("symbol") or "").strip().upper() or None
        # Capped: an unbounded limit from the model would push thousands of
        # headlines into the context window and crowd out the position and
        # risk data the decision actually rests on.
        limit = max(1, min(int(args.get("limit") or 20), 50))
        items = await news_service.recent(db, symbol=symbol, limit=limit)
        return json.dumps(
            {
                "items": [
                    {
                        "symbol": item.symbol,
                        "title": item.title,
                        "source": item.source,
                        "url": item.url,
                        "published_at": item.published_at.isoformat(),
                    }
                    for item in items
                ],
                "note": (
                    "Advisory context only. An empty list means nothing was "
                    "fetched, not that there is no news."
                ),
            }
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
