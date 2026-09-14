"""Broker order postbacks.

Kite posts order state changes to a public URL. Anyone can reach it, so the
payload is only trustworthy if its checksum verifies against the api_secret —
without that, a forged postback could mark an order FILLED that never was, and
the platform would book a position it does not hold.

Reconciliation then applies the verified state to our own order row. Postbacks
arrive out of order and more than once, so it only ever moves an order forward:
a COMPLETE that arrives before an OPEN must not be undone by it.
"""

import hashlib
import hmac
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import BrokerEnvCredentials
from app.core.logging import get_logger
from app.db.models import BrokerAccount, Order
from app.domain.enums import AuditEventType, Broker, OrderStatus
from app.services import audit

logger = get_logger(__name__)


class PostbackError(Exception):
    """Raised when a postback cannot be trusted or applied."""


# Kite's order states mapped onto ours. Anything unrecognised is left alone
# rather than guessed at — a state we do not understand must not silently
# become one we do.
_KITE_STATUS = {
    "COMPLETE": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
    "OPEN": OrderStatus.OPEN,
    "TRIGGER PENDING": OrderStatus.OPEN,
    "VALIDATION PENDING": OrderStatus.ACCEPTED,
    "PUT ORDER REQ RECEIVED": OrderStatus.ACCEPTED,
    "MODIFY VALIDATION PENDING": OrderStatus.OPEN,
    "MODIFY PENDING": OrderStatus.OPEN,
    "CANCEL PENDING": OrderStatus.OPEN,
}

# Terminality is defined on OrderStatus itself; keeping a second list here
# would let the two drift, and this one governs whether a broker can reopen a
# settled order. A fill is not un-filled by a stale OPEN arriving afterwards.


def expected_checksum(order_id: str, order_timestamp: str, api_secret: str) -> str:
    return hashlib.sha256(
        f"{order_id}{order_timestamp}{api_secret}".encode()
    ).hexdigest()


def verify_checksum(payload: dict, api_secret: str) -> bool:
    """Whether this payload really came from Kite.

    SHA256(order_id + order_timestamp + api_secret), compared in constant time
    so the comparison itself does not leak how much of a forged checksum was
    correct.
    """
    supplied = payload.get("checksum")
    order_id = payload.get("order_id")
    order_timestamp = payload.get("order_timestamp")
    if not supplied or not order_id or not order_timestamp or not api_secret:
        return False
    return hmac.compare_digest(
        supplied, expected_checksum(str(order_id), str(order_timestamp), api_secret)
    )


async def _account_for(db: AsyncSession, payload: dict) -> BrokerAccount | None:
    """Find the account this postback belongs to.

    Kite identifies the trading account by user_id, which we store as
    broker_client_id once a profile has been fetched.
    """
    client_id = payload.get("user_id")
    if not client_id:
        return None
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.broker == Broker.ZERODHA.value,
            BrokerAccount.broker_client_id == str(client_id),
        )
    )
    return result.scalar_one_or_none()


async def verify(db: AsyncSession, payload: dict) -> BrokerAccount:
    """Resolve and authenticate a postback, or raise.

    The api_secret comes from the environment keyed by the account's
    credential_ref, so a postback for an account we do not hold credentials
    for is refused rather than trusted.
    """
    account = await _account_for(db, payload)
    if account is None:
        raise PostbackError("No broker account matches this postback")
    credentials = BrokerEnvCredentials(account.credential_ref or "")
    if not credentials.api_secret:
        raise PostbackError("No api secret configured for this account")
    if not verify_checksum(payload, credentials.api_secret):
        raise PostbackError("Checksum does not verify")
    return account


async def reconcile(db: AsyncSession, account: BrokerAccount, payload: dict) -> Order | None:
    """Apply a verified postback to our order row.

    Returns the order if it moved, None if there was nothing to do. Postbacks
    repeat and arrive out of order, so this is deliberately idempotent and
    forward-only.
    """
    broker_order_id = payload.get("order_id")
    if not broker_order_id:
        return None

    result = await db.execute(
        select(Order).where(
            Order.broker_account_id == account.id,
            Order.broker_order_id == str(broker_order_id),
        )
    )
    order = result.scalar_one_or_none()
    if order is None:
        # An order we never placed, or one placed outside the platform. Worth
        # recording but not an error: the webhook already stored the raw event.
        logger.info(
            "postback_for_unknown_order",
            broker_order_id=str(broker_order_id),
            broker_account_id=str(account.id),
        )
        return None

    kite_status = str(payload.get("status", "")).upper()
    new_status = _KITE_STATUS.get(kite_status)
    # Kite reports a partly-filled live order as OPEN with a filled_quantity.
    # Recording that as plain OPEN would lose the fill.
    if new_status == OrderStatus.OPEN:
        try:
            if int(payload.get("filled_quantity") or 0) > 0:
                new_status = OrderStatus.PARTIALLY_FILLED
        except (TypeError, ValueError):
            pass
    if new_status is None:
        logger.warning("postback_unknown_status", status=kite_status, order_id=str(order.id))
        return None

    current = OrderStatus(order.status)
    if current.is_terminal:
        # Already settled. A late or duplicate postback cannot reopen it.
        return None
    if new_status == current:
        return None

    previous = order.status
    order.status = new_status.value
    filled = payload.get("filled_quantity")
    if filled is not None:
        try:
            order.filled_quantity = int(filled)
        except (TypeError, ValueError):
            pass
    average = payload.get("average_price")
    if average is not None:
        try:
            from decimal import Decimal

            order.average_fill_price = Decimal(str(average))
        except (TypeError, ValueError, ArithmeticError):
            pass
    if payload.get("status_message"):
        order.status_message = str(payload["status_message"])[:500]

    await audit.emit(
        db,
        AuditEventType.ORDER_STATE_CHANGED,
        user_id=account.user_id,
        entity_type="order",
        entity_id=order.id,
        correlation_id=order.client_order_id,
        payload={
            "via": "zerodha_postback",
            "from": previous,
            "to": order.status,
            "broker_order_id": str(broker_order_id),
        },
    )
    return order


async def handle(db: AsyncSession, payload: dict) -> uuid.UUID | None:
    """Verify then reconcile. Returns the order id if one moved."""
    account = await verify(db, payload)
    order = await reconcile(db, account, payload)
    return order.id if order is not None else None
