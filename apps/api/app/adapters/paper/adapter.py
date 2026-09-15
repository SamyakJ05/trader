"""Paper broker adapter — STATUS: WORKING.

Implements the BrokerAdapter interface against internal state (Postgres +
simulated feed) so the rest of the system treats paper exactly like a real
broker. Order persistence lives in the order service; fills happen in
app/engines/paper/engine.py. This adapter is the read/quote surface plus
order acknowledgement."""

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.adapters.base import BrokerAdapter, FeatureNotSupportedError
from app.core.redis import get_redis
from app.db.models import Order, PaperHolding, Position
from app.db.session import async_session_factory
from app.domain.enums import (
    Broker,
    Exchange,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
)
from app.domain.models import (
    BrokerOrder,
    BrokerPosition,
    BrokerProfile,
    Funds,
    Holding,
    Instrument,
    OrderRequest,
    PlaceOrderResult,
    Tick,
)
from app.engines.paper import engine as paper_engine
from app.engines.paper import market_sim


class PaperAdapter(BrokerAdapter):
    broker = Broker.PAPER

    async def connect(self) -> dict:
        return {"status": "connected", "flow": "none"}

    async def refresh_session(self) -> dict:
        return {"status": "connected"}

    async def get_profile(self) -> BrokerProfile:
        return BrokerProfile(
            broker_client_id=f"PAPER-{self.account.id.hex[:8].upper()}",
            name=self.account.label,
        )

    async def get_funds(self) -> Funds:
        async with async_session_factory() as db:
            cash = await paper_engine.get_cash(db, self.account.id)
        return Funds(available_cash=cash)

    async def get_holdings(self) -> list[Holding]:
        async with async_session_factory() as db:
            rows = (
                (
                    await db.execute(
                        select(PaperHolding).where(
                            PaperHolding.broker_account_id == self.account.id,
                            PaperHolding.quantity > 0,
                        )
                    )
                )
                .scalars()
                .all()
            )
        redis = get_redis()
        out = []
        for row in rows:
            last = await market_sim.get_price(redis, row.symbol)
            out.append(
                Holding(
                    symbol=row.symbol,
                    exchange=Exchange(row.exchange),
                    quantity=row.quantity,
                    average_price=row.average_price,
                    last_price=last,
                    pnl=(last - row.average_price) * row.quantity,
                )
            )
        return out

    async def get_positions(self) -> list[BrokerPosition]:
        async with async_session_factory() as db:
            result = await db.execute(
                select(Position).where(Position.broker_account_id == self.account.id)
            )
            rows = result.scalars().all()
        redis = get_redis()
        out = []
        for p in rows:
            last = await market_sim.get_price(redis, p.symbol)
            out.append(
                BrokerPosition(
                    symbol=p.symbol,
                    exchange=Exchange(p.exchange),
                    product=ProductType(p.product),
                    quantity=p.quantity,
                    average_price=p.average_price,
                    last_price=last,
                    realized_pnl=p.realized_pnl,
                    unrealized_pnl=(last - p.average_price) * p.quantity,
                )
            )
        return out

    async def get_orders(self) -> list[BrokerOrder]:
        async with async_session_factory() as db:
            result = await db.execute(
                select(Order)
                .where(Order.broker_account_id == self.account.id)
                .order_by(Order.placed_at.desc())
                .limit(200)
            )
            rows = result.scalars().all()
        return [
            BrokerOrder(
                broker_order_id=o.broker_order_id or "",
                symbol=o.symbol,
                exchange=Exchange(o.exchange),
                side=OrderSide(o.side),
                order_type=OrderType(o.order_type),
                product=ProductType(o.product),
                quantity=o.quantity,
                filled_quantity=o.filled_quantity,
                price=o.price,
                average_fill_price=o.average_fill_price,
                status=OrderStatus(o.status),
                status_message=o.status_message,
                placed_at=o.placed_at,
            )
            for o in rows
        ]

    async def place_order(self, request: OrderRequest, client_order_id: str) -> PlaceOrderResult:
        # Acknowledge instantly; the order service persists the row and the
        # paper engine produces fills on the next tick (or inline for markets).
        return PlaceOrderResult(
            broker_order_id=f"PAPER-{uuid.uuid4().hex[:12].upper()}",
            status=OrderStatus.ACCEPTED,
        )

    async def modify_order(self, broker_order_id: str, request: OrderRequest) -> PlaceOrderResult:
        return PlaceOrderResult(broker_order_id=broker_order_id, status=OrderStatus.OPEN)

    async def cancel_order(self, broker_order_id: str) -> PlaceOrderResult:
        return PlaceOrderResult(broker_order_id=broker_order_id, status=OrderStatus.CANCELLED)

    async def get_instruments(self, exchange: str | None = None) -> list[Instrument]:
        raise FeatureNotSupportedError(
            "Paper simulator has no instrument master; any symbol is tradable"
        )

    async def subscribe_ticks(self, symbols: list[str]) -> AsyncIterator[Tick]:
        redis = get_redis()
        import asyncio

        while True:
            for symbol in symbols:
                price = await market_sim.get_price(redis, symbol)
                yield Tick(
                    symbol=symbol,
                    exchange=Exchange.NSE,
                    last_price=Decimal(price),
                    ts=datetime.now(timezone.utc),
                )
            await asyncio.sleep(2)
