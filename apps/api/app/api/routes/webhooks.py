from fastapi import APIRouter, Request

from app.core.deps import DbSession
from app.core.logging import get_logger
from app.db.models import WebhookEvent
from app.domain.enums import AuditEventType, Broker
from app.services import audit, postbacks

logger = get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/zerodha")
async def zerodha_postback(request: Request, db: DbSession):
    """Kite order postback receiver.

    The URL is public, so nothing here is trusted until its checksum verifies
    against the account's api_secret: a forged postback could otherwise mark an
    order filled that never was, and the platform would book a position it does
    not hold.

    Always answers 200. Kite retries anything else, and a postback we cannot
    match or verify will not become matchable on a retry — the raw event is
    stored either way, so nothing is lost.
    """
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
    try:
        order_id = await postbacks.handle(db, payload)
    except postbacks.PostbackError as exc:
        # Refused, not failed: an unverifiable postback is recorded and
        # ignored. Logged at warning because a genuine one failing to verify
        # means credentials have drifted.
        logger.warning("postback_refused", broker="zerodha", reason=str(exc))
        await db.commit()
        return {"status": "ignored"}

    await db.commit()
    return {"status": "reconciled" if order_id else "received"}
