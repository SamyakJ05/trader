import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.deps import DbSession, VerifiedUser
from app.core.redis import get_redis
from app.db.models import Order
from app.domain.enums import Environment
from app.domain.models import OrderRequest
from app.services import brokers as broker_service
from app.services import orders as order_service
from app.services.orders import OrderServiceError

router = APIRouter(prefix="/orders", tags=["orders"])


class PlaceOrderBody(BaseModel):
    broker_account_id: uuid.UUID
    client_order_id: str | None = Field(
        default=None,
        max_length=64,
        description="Idempotency key; replays with the same id return the original order",
    )
    order: OrderRequest


class ModifyOrderBody(BaseModel):
    price: Decimal | None = None
    quantity: int | None = Field(default=None, gt=0)


class OrderOut(BaseModel):
    id: str
    client_order_id: str
    broker_order_id: str | None
    environment: str
    symbol: str
    exchange: str
    side: str
    order_type: str
    product: str
    quantity: int
    filled_quantity: int
    price: Decimal | None
    trigger_price: Decimal | None
    average_fill_price: Decimal | None
    status: str
    status_message: str | None
    strategy_id: str | None
    placed_at: datetime
    updated_at: datetime


def _order_out(o: Order) -> OrderOut:
    return OrderOut(
        id=str(o.id),
        client_order_id=o.client_order_id,
        broker_order_id=o.broker_order_id,
        environment=o.environment,
        symbol=o.symbol,
        exchange=o.exchange,
        side=o.side,
        order_type=o.order_type,
        product=o.product,
        quantity=o.quantity,
        filled_quantity=o.filled_quantity,
        price=o.price,
        trigger_price=o.trigger_price,
        average_fill_price=o.average_fill_price,
        status=o.status,
        status_message=o.status_message,
        strategy_id=str(o.strategy_id) if o.strategy_id else None,
        placed_at=o.placed_at,
        updated_at=o.updated_at,
    )


@router.get("", response_model=list[OrderOut])
async def list_orders(
    user: VerifiedUser,
    db: DbSession,
    environment: Environment | None = None,
    status_filter: str | None = None,
    limit: int = 100,
):
    query = select(Order).where(Order.user_id == user.id)
    if environment:
        query = query.where(Order.environment == environment.value)
    if status_filter:
        query = query.where(Order.status == status_filter)
    query = query.order_by(Order.placed_at.desc()).limit(min(limit, 500))
    result = await db.execute(query)
    return [_order_out(o) for o in result.scalars()]


@router.post("", response_model=OrderOut, status_code=201)
async def place_order(
    body: PlaceOrderBody,
    user: VerifiedUser,
    db: DbSession,
    idempotency_key: str | None = Header(default=None),
):
    account = await broker_service.get_account(db, user.id, body.broker_account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Broker account not found")

    client_order_id = body.client_order_id or idempotency_key or f"ord-{uuid.uuid4().hex[:16]}"
    order = await order_service.place_order(
        db,
        get_redis(),
        user_id=user.id,
        account=account,
        request=body.order,
        client_order_id=client_order_id,
    )
    return _order_out(order)


async def _owned_order(db: DbSession, user: VerifiedUser, order_id: uuid.UUID) -> Order:
    result = await db.execute(
        select(Order).where(Order.id == order_id, Order.user_id == user.id)
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    return order


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: uuid.UUID, user: VerifiedUser, db: DbSession):
    return _order_out(await _owned_order(db, user, order_id))


@router.post("/{order_id}/cancel", response_model=OrderOut)
async def cancel_order(order_id: uuid.UUID, user: VerifiedUser, db: DbSession):
    order = await _owned_order(db, user, order_id)
    try:
        order = await order_service.cancel_order(db, get_redis(), user_id=user.id, order=order)
    except OrderServiceError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    return _order_out(order)


@router.patch("/{order_id}", response_model=OrderOut)
async def modify_order(
    order_id: uuid.UUID, body: ModifyOrderBody, user: VerifiedUser, db: DbSession
):
    order = await _owned_order(db, user, order_id)
    try:
        order = await order_service.modify_order(
            db, get_redis(), user_id=user.id, order=order, price=body.price, quantity=body.quantity
        )
    except OrderServiceError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    return _order_out(order)
