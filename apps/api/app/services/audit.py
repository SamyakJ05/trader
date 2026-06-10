"""Append-only audit trail. Every significant action emits an event here:
signals, risk checks, order lifecycle, broker responses, user actions.
Events are never updated or deleted."""

import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditEvent
from app.domain.enums import AuditEventType


def jsonable(data: Any) -> Any:
    """Coerce Decimals/UUIDs/datetimes into JSON-safe values."""
    return json.loads(json.dumps(data, default=str))


async def emit(
    db: AsyncSession,
    event_type: AuditEventType,
    *,
    user_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    correlation_id: str | None = None,
    payload: dict | None = None,
) -> None:
    """Stage an audit event on the session. Caller owns the commit so the
    event is atomic with the state change it describes."""
    db.add(
        AuditEvent(
            event_type=event_type.value,
            user_id=user_id,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id else None,
            correlation_id=correlation_id,
            payload=jsonable(payload or {}),
        )
    )
