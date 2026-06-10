from fastapi import APIRouter, Request

from app.core.deps import DbSession
from app.db.models import WebhookEvent
from app.domain.enums import AuditEventType, Broker
from app.services import audit

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/zerodha")
async def zerodha_postback(request: Request, db: DbSession):
    """Kite Connect order postback receiver. SCAFFOLD: stores the event for
    the audit trail; checksum validation and order-state reconciliation are
    TODO before live use.

    TODO(zerodha-postback): validate checksum = SHA256(order_id + order_timestamp
    + api_secret), then update the matching orders row by broker_order_id."""
    try:
        payload = await request.json()
    except Exception:
        payload = {"raw": (await request.body()).decode(errors="replace")}

    event = WebhookEvent(
        broker=Broker.ZERODHA.value,
        headers=dict(request.headers),
        payload=payload,
    )
    db.add(event)
    await audit.emit(
        db,
        AuditEventType.WEBHOOK_RECEIVED,
        entity_type="webhook",
        payload={"broker": "zerodha", "order_id": payload.get("order_id")},
    )
    await db.commit()
    return {"status": "received"}
