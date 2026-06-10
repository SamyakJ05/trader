"""Risk engine. Evaluates every order intent against the user's enabled
risk rules before it can reach any broker (paper included). Decisions:
ALLOW, BLOCK (this order), HALT (kill switch / daily loss breach).
Every evaluation is persisted as a risk_event and audited."""

import uuid
from datetime import datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import BrokerAccount, Position, RiskEvent, RiskRule
from app.domain.enums import (
    AuditEventType,
    OrderSide,
    RiskDecision,
    RiskRuleType,
)
from app.domain.models import OrderRequest, RiskResult
from app.services import audit, daily_pnl, killswitch

IST = ZoneInfo("Asia/Kolkata")
NSE_OPEN = time(9, 15)
NSE_CLOSE = time(15, 30)

COOLDOWN_KEY = "risk:cooldown:{account_id}:{symbol}:{side}"


def is_market_open(now: datetime | None = None) -> bool:
    """NSE equity hours, Mon-Fri 09:15-15:30 IST.
    TODO(holidays): wire an exchange holiday calendar."""
    now_ist = (now or datetime.now(timezone.utc)).astimezone(IST)
    if now_ist.weekday() >= 5:
        return False
    return NSE_OPEN <= now_ist.time() <= NSE_CLOSE


class RiskEngine:
    def __init__(self, db: AsyncSession, redis: aioredis.Redis):
        self.db = db
        self.redis = redis

    async def evaluate(
        self,
        *,
        user_id: uuid.UUID,
        account: BrokerAccount,
        request: OrderRequest,
        environment: str,
        last_price: Decimal,
        client_order_id: str,
        strategy_id: uuid.UUID | None = None,
    ) -> RiskResult:
        reasons: list[str] = []
        checked: list[str] = []
        decision = RiskDecision.ALLOW

        # Kill switches first — cheapest and most absolute.
        if await killswitch.is_global_engaged(self.redis):
            decision = RiskDecision.HALT
            reasons.append("Global kill switch engaged")
        elif strategy_id and await killswitch.is_strategy_engaged(self.redis, strategy_id):
            decision = RiskDecision.HALT
            reasons.append(f"Kill switch engaged for strategy {strategy_id}")

        rules = await self._load_rules(user_id, environment)

        if decision == RiskDecision.ALLOW:
            for rule in rules:
                rule_type = RiskRuleType(rule.rule_type)
                checked.append(rule_type.value)
                reason = await self._check_rule(
                    rule_type, rule.params, user_id, account, request, environment, last_price
                )
                if reason:
                    reasons.append(reason)
                    decision = (
                        RiskDecision.HALT
                        if rule_type == RiskRuleType.MAX_DAILY_LOSS
                        else RiskDecision.BLOCK
                    )
                    break

        if decision == RiskDecision.ALLOW:
            await self._arm_cooldown(account, request, rules)

        result = RiskResult(decision=decision.value, reasons=reasons, checked_rules=checked)
        await self._record(user_id, environment, decision, reasons, request, client_order_id)
        return result

    async def _load_rules(self, user_id: uuid.UUID, environment: str) -> list[RiskRule]:
        result = await self.db.execute(
            select(RiskRule).where(
                RiskRule.user_id == user_id,
                RiskRule.environment == environment,
                RiskRule.enabled.is_(True),
            )
        )
        return list(result.scalars())

    async def _check_rule(
        self,
        rule_type: RiskRuleType,
        params: dict,
        user_id: uuid.UUID,
        account: BrokerAccount,
        request: OrderRequest,
        environment: str,
        last_price: Decimal | None,
    ) -> str | None:
        if rule_type == RiskRuleType.MARKET_HOURS:
            if get_settings().market_hours_enforced and not is_market_open():
                return "Outside NSE market hours (09:15-15:30 IST, Mon-Fri)"

        elif rule_type == RiskRuleType.MAX_ORDER_NOTIONAL:
            limit = Decimal(str(params.get("max_notional", 100000)))
            reference = request.price or last_price
            if reference is None:
                # Cannot value the order; refuse rather than guess.
                return "No reference price available to value order notional"
            notional = reference * request.quantity
            if notional > limit:
                return f"Order notional {notional:.2f} exceeds limit {limit:.2f}"

        elif rule_type == RiskRuleType.MAX_POSITION_SIZE:
            limit_qty = int(params.get("max_quantity", 1000))
            result = await self.db.execute(
                select(Position).where(
                    Position.broker_account_id == account.id,
                    Position.symbol == request.symbol,
                    Position.exchange == request.exchange.value,
                    Position.product == request.product.value,
                )
            )
            position = result.scalar_one_or_none()
            current = position.quantity if position else 0
            delta = request.quantity if request.side == OrderSide.BUY else -request.quantity
            if abs(current + delta) > limit_qty:
                return (
                    f"Resulting position {current + delta} in {request.symbol} "
                    f"exceeds max size {limit_qty}"
                )

        elif rule_type == RiskRuleType.MAX_OPEN_POSITIONS:
            limit_n = int(params.get("max_positions", 10))
            result = await self.db.execute(
                select(func.count())
                .select_from(Position)
                .where(
                    Position.user_id == user_id,
                    Position.environment == environment,
                    Position.quantity != 0,
                )
            )
            if result.scalar_one() >= limit_n:
                return f"Open positions at limit ({limit_n})"

        elif rule_type == RiskRuleType.MAX_DAILY_LOSS:
            limit_loss = Decimal(str(params.get("max_loss", 10000)))
            # Per-day realized delta tracked at fill time (positions store
            # cumulative P&L, which cannot answer the daily question).
            pnl = await daily_pnl.get_realized(self.redis, user_id, environment)
            if pnl < -limit_loss:
                return f"Daily realized loss {pnl:.2f} breaches limit -{limit_loss:.2f}"

        elif rule_type == RiskRuleType.DUPLICATE_ORDER_COOLDOWN:
            key = COOLDOWN_KEY.format(
                account_id=account.id, symbol=request.symbol, side=request.side.value
            )
            if await self.redis.exists(key):
                ttl = await self.redis.ttl(key)
                return f"Duplicate-order cooldown active for {request.symbol} {request.side} ({ttl}s left)"

        return None

    async def _arm_cooldown(
        self, account: BrokerAccount, request: OrderRequest, rules: list[RiskRule]
    ) -> None:
        rule = next(
            (r for r in rules if r.rule_type == RiskRuleType.DUPLICATE_ORDER_COOLDOWN.value), None
        )
        if rule is None:
            return
        seconds = int(rule.params.get("seconds", 10))
        key = COOLDOWN_KEY.format(
            account_id=account.id, symbol=request.symbol, side=request.side.value
        )
        await self.redis.set(key, "1", ex=seconds)

    async def _record(
        self,
        user_id: uuid.UUID,
        environment: str,
        decision: RiskDecision,
        reasons: list[str],
        request: OrderRequest,
        client_order_id: str,
    ) -> None:
        self.db.add(
            RiskEvent(
                user_id=user_id,
                environment=environment,
                rule_type=None,
                decision=decision.value,
                reason="; ".join(reasons) or "all checks passed",
                context=audit.jsonable(
                    {"order": request.model_dump(), "client_order_id": client_order_id}
                ),
            )
        )
        await audit.emit(
            self.db,
            AuditEventType.RISK_CHECK,
            user_id=user_id,
            entity_type="order_intent",
            entity_id=client_order_id,
            correlation_id=client_order_id,
            payload={"decision": decision.value, "reasons": reasons},
        )
