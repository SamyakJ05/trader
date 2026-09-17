"""user_remark, and what the error says when an order is refused.

Both found by placing a real order. Breeze answered:

    Only alphanumeric characters are allowed in user_remark

Every client_order_id this platform generates carries a hyphen -- ord-...,
ai-..., cli-fo-... -- so the field refused every order that reached the API.
The message also arrived with a static-IP hint appended, which pointed away
from the field Breeze had just named.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.adapters.base import BrokerError
from app.adapters.icici_breeze.adapter import (
    BreezeAdapter,
    _names_its_own_cause,
    _user_remark,
)
from app.domain.enums import Exchange, OrderSide, OrderType, ProductType, Validity
from app.domain.models import OrderRequest

# ── the field itself ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "client_order_id",
    [
        "ord-3f9c1a2b4d5e6f70",  # the manual route
        "ai-7b2c9d1e4f0a",       # an approved or auto-executed proposal
        "cli-fo-1",              # the playbook's F&O example
        "a_b.c/d",
    ],
)
def test_every_generated_id_becomes_alphanumeric(client_order_id):
    remark = _user_remark(client_order_id)
    assert remark.isalnum(), f"{remark!r} would be refused by Breeze"
    assert len(remark) <= 20


def test_stripping_happens_before_truncation():
    """Truncating first would spend the budget on characters about to be
    removed, and could leave only the prefix -- 'ord' for every order."""
    assert _user_remark("ord-3f9c1a2b4d5e6f70") == "ord3f9c1a2b4d5e6f70"


def test_an_id_with_nothing_left_still_sends_something():
    """An empty user_remark is of unknown acceptability to Breeze; a constant
    is known to be safe."""
    assert _user_remark("---") == "trader"
    assert _user_remark("") == "trader"


def test_the_order_body_carries_a_clean_remark():
    account = SimpleNamespace(
        broker="icici_breeze",
        credential_ref="BREEZE_MAIN",
        broker_client_id="X1",
        session_token_enc=None,
        environment="live",
    )
    adapter = BreezeAdapter.__new__(BreezeAdapter)
    adapter.account = account
    body = adapter._order_body(
        OrderRequest(
            symbol="RELIND",
            exchange=Exchange.NSE,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            product=ProductType.CNC,
            quantity=1,
            price=Decimal("995"),
            validity=Validity.DAY,
        ),
        "ord-3f9c1a2b4d5e6f70",
    )
    assert body["user_remark"].isalnum()
    # The rest of the order must be untouched by the sanitising.
    assert body["stock_code"] == "RELIND"
    assert body["quantity"] == "1"
    assert body["price"] == "995"


# ── what the failure message says ────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "Breeze error: Only alphanumeric characters are allowed in user_remark",
        "Breeze error: Insufficient margin",
        "Breeze error: Market is closed",
        "Breeze error: Invalid stock_code",
    ],
)
def test_a_message_that_explains_itself_gets_no_ip_theory(message):
    assert _names_its_own_cause(message)


@pytest.mark.parametrize(
    "message",
    [
        "Breeze error: Request failed",
        "Breeze error: {}",
        "Breeze error: Unable to process",
    ],
)
def test_an_opaque_message_still_gets_the_ip_hint(message):
    """The hint earns its place on exactly these: a blocked-by-IP rejection
    arrives as an ordinary error with nothing distinguishing about it, and
    suspicion falls on the session and the payload first."""
    assert not _names_its_own_cause(message)


async def test_place_order_does_not_bury_breeze_s_own_reason(monkeypatch):
    """The regression: the real rejection named user_remark, and the IP hint
    was appended anyway -- sending the reader to check the network path
    instead of the field the broker had just identified."""
    adapter = BreezeAdapter.__new__(BreezeAdapter)
    adapter.account = SimpleNamespace(
        broker="icici_breeze",
        credential_ref="BREEZE_MAIN",
        broker_client_id="X1",
        session_token_enc=None,
        environment="live",
    )

    async def no_throttle():
        return None

    async def refuse(*a, **kw):
        raise BrokerError(
            "Breeze error: Only alphanumeric characters are allowed in user_remark"
        )

    monkeypatch.setattr(adapter, "_throttle_order", no_throttle)
    monkeypatch.setattr(adapter, "_request", refuse)

    with pytest.raises(BrokerError) as exc:
        await adapter.place_order(
            OrderRequest(
                symbol="RELIND",
                exchange=Exchange.NSE,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                product=ProductType.CNC,
                quantity=1,
                price=Decimal("995"),
                validity=Validity.DAY,
            ),
            "ord-abc123",
        )
    assert "user_remark" in str(exc.value)
    assert "static IP" not in str(exc.value)
