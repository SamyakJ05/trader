from fastapi import APIRouter
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession
from app.db.models import AuditEvent

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/events")
async def list_events(
    user: CurrentUser,
    db: DbSession,
    event_type: str | None = None,
    entity_type: str | None = None,
    correlation_id: str | None = None,
    before_id: int | None = None,
    limit: int = 100,
):
    """Cursor-paginated audit stream (newest first). Scoped to the caller."""
    query = select(AuditEvent).where(AuditEvent.user_id == user.id)
    if event_type:
        query = query.where(AuditEvent.event_type == event_type)
    if entity_type:
        query = query.where(AuditEvent.entity_type == entity_type)
    if correlation_id:
        query = query.where(AuditEvent.correlation_id == correlation_id)
    if before_id:
        query = query.where(AuditEvent.id < before_id)
    query = query.order_by(AuditEvent.id.desc()).limit(min(limit, 500))
    result = await db.execute(query)
    events = result.scalars().all()
    return {
        "events": [
            {
                "id": e.id,
                "ts": e.ts.isoformat(),
                "event_type": e.event_type,
                "entity_type": e.entity_type,
                "entity_id": e.entity_id,
                "correlation_id": e.correlation_id,
                "payload": e.payload,
            }
            for e in events
        ],
        "next_cursor": events[-1].id if events else None,
    }
