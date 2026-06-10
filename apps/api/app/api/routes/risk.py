import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession
from app.db.models import RiskEvent, RiskRule
from app.domain.enums import AuditEventType, Environment, RiskRuleType
from app.services import audit

router = APIRouter(prefix="/risk", tags=["risk"])


class RuleBody(BaseModel):
    rule_type: RiskRuleType
    environment: Environment = Environment.PAPER
    params: dict = Field(default_factory=dict)
    enabled: bool = True


class RuleOut(BaseModel):
    id: str
    rule_type: str
    environment: str
    params: dict
    enabled: bool


def _out(r: RiskRule) -> RuleOut:
    return RuleOut(
        id=str(r.id),
        rule_type=r.rule_type,
        environment=r.environment,
        params=r.params,
        enabled=r.enabled,
    )


@router.get("/rules", response_model=list[RuleOut])
async def list_rules(user: CurrentUser, db: DbSession):
    result = await db.execute(
        select(RiskRule).where(RiskRule.user_id == user.id).order_by(RiskRule.created_at)
    )
    return [_out(r) for r in result.scalars()]


@router.post("/rules", response_model=RuleOut, status_code=201)
async def create_rule(body: RuleBody, user: CurrentUser, db: DbSession):
    rule = RiskRule(
        user_id=user.id,
        rule_type=body.rule_type.value,
        environment=body.environment.value,
        params=body.params,
        enabled=body.enabled,
    )
    db.add(rule)
    await audit.emit(
        db, AuditEventType.USER_ACTION, user_id=user.id,
        entity_type="risk_rule", payload={"action": "create", "rule": body.model_dump(mode="json")},
    )
    await db.commit()
    return _out(rule)


@router.patch("/rules/{rule_id}", response_model=RuleOut)
async def update_rule(rule_id: uuid.UUID, body: RuleBody, user: CurrentUser, db: DbSession):
    result = await db.execute(
        select(RiskRule).where(RiskRule.id == rule_id, RiskRule.user_id == user.id)
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    rule.rule_type = body.rule_type.value
    rule.environment = body.environment.value
    rule.params = body.params
    rule.enabled = body.enabled
    await audit.emit(
        db, AuditEventType.USER_ACTION, user_id=user.id,
        entity_type="risk_rule", entity_id=rule.id,
        payload={"action": "update", "rule": body.model_dump(mode="json")},
    )
    await db.commit()
    return _out(rule)


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: uuid.UUID, user: CurrentUser, db: DbSession):
    result = await db.execute(
        select(RiskRule).where(RiskRule.id == rule_id, RiskRule.user_id == user.id)
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    await db.delete(rule)
    await audit.emit(
        db, AuditEventType.USER_ACTION, user_id=user.id,
        entity_type="risk_rule", entity_id=rule_id, payload={"action": "delete"},
    )
    await db.commit()


@router.get("/events")
async def risk_events(user: CurrentUser, db: DbSession, limit: int = 100):
    result = await db.execute(
        select(RiskEvent)
        .where(RiskEvent.user_id == user.id)
        .order_by(RiskEvent.ts.desc())
        .limit(min(limit, 500))
    )
    return [
        {
            "id": str(e.id),
            "ts": e.ts.isoformat(),
            "environment": e.environment,
            "decision": e.decision,
            "reason": e.reason,
            "context": e.context,
        }
        for e in result.scalars()
    ]
