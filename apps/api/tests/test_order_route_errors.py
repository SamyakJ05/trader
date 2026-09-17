"""What the order route tells the person when it refuses.

Both cases here were found by placing a real order: an NSE ticker was typed
where the broker's own code was needed, and the UI showed an empty red box.
Two separate faults, and the second hid the first -- the reason reached the
server log and nothing else.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routes import orders as order_routes
from app.domain.enums import Exchange, OrderSide, OrderType, ProductType
from app.domain.models import OrderRequest
from app.services.orders import OrderServiceError


def body(symbol="RELIND"):
    return SimpleNamespace(
        broker_account_id=uuid.uuid4(),
        client_order_id=None,
        order=OrderRequest(
            symbol=symbol,
            exchange=Exchange.NSE,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            product=ProductType.CNC,
            quantity=1,
            price=995,
        ),
    )


def account():
    return SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), broker="icici_breeze", environment="live"
    )


@pytest.fixture
def user():
    return SimpleNamespace(id=uuid.uuid4())


async def test_an_unrecognised_symbol_is_refused_by_name(monkeypatch, user):
    """RELIANCE on a Breeze account is a mistake this route can name.

    It used to reach the risk engine, find no quote -- because Breeze streams
    RELIND and never RELIANCE -- and fail as "no recent market price", which
    describes a broken feed and sends someone to check the tick stream.
    """
    monkeypatch.setattr(
        order_routes.broker_service, "get_account", _returns(account())
    )
    monkeypatch.setattr(
        order_routes.instrument_service, "unknown_symbols", _returns(["RELIANCE"])
    )

    async def must_not_be_called(*a, **kw):  # pragma: no cover
        raise AssertionError("an unknown symbol must not reach the order pipeline")

    monkeypatch.setattr(order_routes.order_service, "place_order", must_not_be_called)

    with pytest.raises(HTTPException) as exc:
        await order_routes.place_order(
            body("RELIANCE"), user, None, idempotency_key=None
        )
    assert exc.value.status_code == 422
    assert "does not recognise RELIANCE" in exc.value.detail
    # The fix, not just the diagnosis: the message has to say what to type.
    assert "RELIND" in exc.value.detail


async def test_a_refusal_reaches_the_client_instead_of_a_bare_500(monkeypatch, user):
    """OrderServiceError was uncaught here, though modify and cancel both map
    it. It escaped as a 500 with no body, so the UI showed an empty red box
    and the reason existed only in the server log."""
    monkeypatch.setattr(
        order_routes.broker_service, "get_account", _returns(account())
    )
    monkeypatch.setattr(
        order_routes.instrument_service, "unknown_symbols", _returns([])
    )

    async def refuse(*a, **kw):
        raise OrderServiceError(
            "No recent market price for RELIND on NSE. A live order cannot be "
            "risk-checked without one."
        )

    monkeypatch.setattr(order_routes.order_service, "place_order", refuse)

    with pytest.raises(HTTPException) as exc:
        await order_routes.place_order(body(), user, None, idempotency_key=None)
    assert exc.value.status_code == 409
    assert "No recent market price" in exc.value.detail


def _returns(value):
    async def _inner(*a, **kw):
        return value

    return _inner
