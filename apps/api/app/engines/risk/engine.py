"""Risk engine. Evaluates every order intent against the user's enabled
risk rules before it can reach any broker (paper included). Decisions:
ALLOW, BLOCK (this order), HALT (kill switch / daily loss breach).
Every evaluation is persisted as a risk_event and audited."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import redis.asyncio as aioredis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import (
    BrokerAccount,
    Fill,
    Order,
    PaperHolding,
    Position,
    RiskEvent,
    RiskRule,
    User,
)
from app.domain import calendar
from app.domain.enums import (
    AuditEventType,
    OrderSide,
    RiskDecision,
    RiskRuleType,
)
from app.domain.models import OrderRequest, RiskResult
from app.services import audit, daily_pnl, killswitch

# Kept as aliases: tests and other callers import them from here.
IST = calendar.IST
NSE_OPEN = calendar.NSE_OPEN
NSE_CLOSE = calendar.NSE_CLOSE

COOLDOWN_KEY = "risk:cooldown:{account_id}:{symbol}:{side}"


async def lock_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Serialize risk evaluation for one user until the caller commits.

    The portfolio rules -- exposure, turnover, open positions, daily loss --
    are read-then-act: each sums what is already committed and compares the
    total against a ceiling. Without a lock, concurrent orders each read the
    same "before" figure and each conclude there is room, so N orders that are
    individually under the ceiling pass together and land over it. A 200k
    exposure limit with 150k held admits five concurrent 40k buys, every one
    of which measured 190k.

    Locked on the USER row, not the account: these ceilings span every account
    in an environment, so an account-level lock would let two accounts of the
    same user race against the same budget. This is the same FOR NO KEY UPDATE
    the paper ledger takes on the account, for the same reason and with the
    same lifetime -- it is held until the transaction ends, which is after the
    order row is written, so a concurrent evaluation sees the committed order.

    Ordering note: this lock is taken BEFORE the paper engine's per-account
    lock on the fill path, and nothing takes them in the opposite order, so
    the pair cannot deadlock.
    """
    await db.execute(select(User.id).where(User.id == user_id).with_for_update(key_share=True))


# Re-exported so existing callers and tests keep working; the calendar module
# owns the definition now that it also has to answer settlement questions.
is_market_open = calendar.is_market_open


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
        auto_executed: bool = False,
    ) -> RiskResult:
        reasons: list[str] = []
        checked: list[str] = []
        decision = RiskDecision.ALLOW

        # Held until the caller commits, so every portfolio rule below reads a
        # figure no concurrent order can be adding to. See lock_user.
        await lock_user(self.db, user_id)

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
                    rule_type,
                    rule.params,
                    user_id,
                    account,
                    request,
                    environment,
                    last_price,
                    auto_executed,
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
        auto_executed: bool = False,
    ) -> str | None:
        if rule_type == RiskRuleType.MARKET_HOURS:
            if get_settings().market_hours_enforced and not is_market_open():
                return "Outside NSE trading hours (09:15-15:30 IST on a trading day)"

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
            if environment == "paper" and request.product.value == "CNC":
                holding = (
                    await self.db.execute(
                        select(PaperHolding).where(
                            PaperHolding.broker_account_id == account.id,
                            PaperHolding.symbol == request.symbol,
                            PaperHolding.exchange == request.exchange.value,
                        )
                    )
                ).scalar_one_or_none()
                current += holding.quantity if holding else 0
            delta = request.quantity if request.side == OrderSide.BUY else -request.quantity
            if abs(current + delta) > limit_qty:
                return (
                    f"Resulting position {current + delta} in {request.symbol} "
                    f"exceeds max size {limit_qty}"
                )

        elif rule_type == RiskRuleType.MAX_TOTAL_EXPOSURE:
            # What is held at once, across every symbol and account in this
            # environment. Neither MAX_ORDER_NOTIONAL nor MAX_POSITION_SIZE
            # bounds this: ten orders of 50k in ten different symbols pass
            # both while committing 5 lakh.
            #
            # Only a BUY consumes room. A sell reduces exposure, and refusing
            # one because the book is full would trap a strategy in exactly
            # the position the limit exists to bound.
            if request.side != OrderSide.BUY:
                return None
            limit = Decimal(str(params.get("max_exposure", 200000)))
            reference = request.price or last_price
            if reference is None:
                return "No reference price available to value total exposure"

            # Valued at average cost rather than at the last price: a mark
            # that moves would let a limit pass or fail on market noise rather
            # than on anything the operator did, and the cost basis is what
            # was actually committed.
            held = (
                await self.db.execute(
                    select(
                        func.coalesce(
                            func.sum(
                                func.abs(Position.quantity) * Position.average_price
                            ),
                            0,
                        )
                    ).where(
                        Position.user_id == user_id,
                        Position.environment == environment,
                        Position.quantity != 0,
                    )
                )
            ).scalar_one()
            if environment == "paper":
                held += (
                    await self.db.execute(
                        select(
                            func.coalesce(
                                func.sum(
                                    PaperHolding.quantity * PaperHolding.average_price
                                ),
                                0,
                            )
                        )
                        .join(BrokerAccount)
                        .where(
                            BrokerAccount.user_id == user_id,
                            PaperHolding.quantity != 0,
                        )
                    )
                ).scalar_one()

            incoming = reference * request.quantity
            if Decimal(str(held)) + incoming > limit:
                return (
                    f"Total exposure would reach {Decimal(str(held)) + incoming:.2f}, "
                    f"over the {limit:.2f} ceiling (currently {Decimal(str(held)):.2f} "
                    "held). Close a position or raise the limit."
                )

        elif rule_type == RiskRuleType.MAX_DAILY_TURNOVER:
            # Every buy today, whether or not it was sold again. This is what
            # bounds a strategy churning the same capital repeatedly -- which
            # exposure alone does not, since each round trip frees its own
            # room and the charges accumulate regardless.
            if request.side != OrderSide.BUY:
                return None
            limit = Decimal(str(params.get("max_turnover", 500000)))
            reference = request.price or last_price
            if reference is None:
                return "No reference price available to value daily turnover"

            # The IST trading day, not a UTC one: a UTC midnight boundary
            # would reset the counter at 05:30 IST, in the middle of the
            # pre-open, and split one trading session across two budgets.
            start = datetime.now(calendar.IST).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).astimezone(timezone.utc)
            # Fills, not orders: an order that was rejected or never filled
            # committed no capital, and counting it would let a run of
            # rejections exhaust the day's budget.
            bought = (
                await self.db.execute(
                    select(
                        func.coalesce(func.sum(Fill.quantity * Fill.price), 0)
                    )
                    .join(Order, Fill.order_id == Order.id)
                    .where(
                        Order.user_id == user_id,
                        Order.environment == environment,
                        Order.side == OrderSide.BUY.value,
                        Fill.ts >= start,
                    )
                )
            ).scalar_one()

            incoming = reference * request.quantity
            if Decimal(str(bought)) + incoming > limit:
                return (
                    f"Daily turnover would reach {Decimal(str(bought)) + incoming:.2f}, "
                    f"over the {limit:.2f} ceiling ({Decimal(str(bought)):.2f} bought "
                    "today). The budget resets at midnight IST."
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
            count = result.scalar_one()
            if environment == "paper":
                # A scrip may have both pending buys and settled shares; count once.
                count += (
                    await self.db.execute(
                        select(func.count())
                        .select_from(PaperHolding)
                        .join(BrokerAccount)
                        .where(
                            BrokerAccount.user_id == user_id,
                            PaperHolding.quantity > 0,
                            ~select(Position.id)
                            .where(
                                Position.broker_account_id == PaperHolding.broker_account_id,
                                Position.symbol == PaperHolding.symbol,
                                Position.exchange == PaperHolding.exchange,
                                Position.product == "CNC",
                                Position.quantity != 0,
                            )
                            .exists(),
                        )
                    )
                ).scalar_one()
            if count >= limit_n:
                existing = (
                    await self.db.execute(
                        select(Position.id)
                        .where(
                            Position.broker_account_id == account.id,
                            Position.symbol == request.symbol,
                            Position.exchange == request.exchange.value,
                            Position.product == request.product.value,
                            Position.quantity != 0,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if existing is None and environment == "paper" and request.product.value == "CNC":
                    existing = (
                        await self.db.execute(
                            select(PaperHolding.id)
                            .where(
                                PaperHolding.broker_account_id == account.id,
                                PaperHolding.symbol == request.symbol,
                                PaperHolding.exchange == request.exchange.value,
                                PaperHolding.quantity > 0,
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                if existing is None:
                    return f"Open positions at limit ({limit_n})"

        elif rule_type == RiskRuleType.MAX_DAILY_LOSS:
            limit_loss = Decimal(str(params.get("max_loss", 10000)))
            # Per-day realized delta tracked at fill time (positions store
            # cumulative P&L, which cannot answer the daily question).
            # The db is passed so a cold cache falls back to the ledger rather
            # than reading zero — a flushed Redis must not look like a day
            # with no losses.
            pnl = await daily_pnl.get_realized(
                self.redis, user_id, environment, self.db
            )
            if pnl < -limit_loss:
                return f"Daily realized loss {pnl:.2f} breaches limit -{limit_loss:.2f}"

        elif rule_type == RiskRuleType.MAX_AUTO_TRADES_PER_DAY:
            # Only bounds unattended orders. A person clicking approve has
            # already applied the judgement this rule exists to substitute
            # for, so rate-limiting them would be the wrong restriction.
            if not auto_executed:
                return None
            limit_n = int(params.get("max_auto_trades", 10))
            # The IST trading day, matching MAX_DAILY_TURNOVER: a UTC boundary
            # would reset the budget at 05:30 IST, mid pre-open.
            start = (
                datetime.now(calendar.IST)
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .astimezone(timezone.utc)
            )
            # Orders, not fills: an auto order that was placed and rejected by
            # the broker still represents the loop having fired, which is what
            # this bounds. Counting fills would let a broken strategy retry
            # without limit as long as nothing filled.
            placed = (
                await self.db.execute(
                    select(func.count())
                    .select_from(Order)
                    .where(
                        Order.user_id == user_id,
                        Order.environment == environment,
                        Order.auto_executed.is_(True),
                        Order.placed_at >= start,
                    )
                )
            ).scalar_one()
            if placed >= limit_n:
                return (
                    f"Daily automatic-trade limit reached ({placed} of {limit_n} placed "
                    "today). Approve trades manually, or raise the limit. The budget "
                    "resets at midnight IST."
                )

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
