"""Seed: demo user, paper broker account, default risk rules, sample strategy.

Run: python -m app.seeds.seed  (idempotent)
Demo login: demo@trader.local / demo1234
"""

import asyncio

from sqlalchemy import select

from app.core.security import hash_password
from app.db.models import BrokerAccount, BrokerCapability, RiskRule, Strategy, User
from app.db.session import async_session_factory
from app.domain.capabilities import CAPABILITY_MATRIX
from app.domain.enums import Broker, Environment, RiskRuleType, StrategyStatus

DEMO_EMAIL = "demo@trader.local"
DEMO_PASSWORD = "demo1234"

DEFAULT_RULES = [
    (RiskRuleType.MAX_DAILY_LOSS, {"max_loss": 10000}),
    (RiskRuleType.MAX_ORDER_NOTIONAL, {"max_notional": 200000}),
    (RiskRuleType.MAX_POSITION_SIZE, {"max_quantity": 500}),
    (RiskRuleType.MAX_OPEN_POSITIONS, {"max_positions": 10}),
    (RiskRuleType.DUPLICATE_ORDER_COOLDOWN, {"seconds": 5}),
    (RiskRuleType.MARKET_HOURS, {}),
]


async def seed() -> None:
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.email == DEMO_EMAIL))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(
                email=DEMO_EMAIL,
                password_hash=hash_password(DEMO_PASSWORD),
                full_name="Demo Trader",
                # The seeded local user is the instance operator, so the
                # global kill switch stays reachable in a dev setup.
                is_admin=True,
            )
            db.add(user)
            await db.flush()
            print(f"created user {DEMO_EMAIL} (password: {DEMO_PASSWORD})")
        else:
            # Idempotent re-seed of an existing database: the operator flag is
            # backfilled to false by migration 0005, so promote here too.
            # Without this, an upgraded instance has zero admins and the
            # global kill switch — a break-glass safety control — is
            # unreachable by anyone.
            if not user.is_admin:
                user.is_admin = True
                print(f"user {DEMO_EMAIL} exists — promoted to operator (is_admin)")
            else:
                print(f"user {DEMO_EMAIL} exists")

        result = await db.execute(
            select(BrokerAccount).where(
                BrokerAccount.user_id == user.id, BrokerAccount.broker == Broker.PAPER.value
            )
        )
        account = result.scalar_one_or_none()
        if account is None:
            account = BrokerAccount(
                user_id=user.id,
                broker=Broker.PAPER.value,
                label="Paper Account",
                environment=Environment.PAPER.value,
                status="connected",
            )
            db.add(account)
            await db.flush()
            print("created paper broker account")

        existing_rules = await db.execute(select(RiskRule).where(RiskRule.user_id == user.id))
        if not existing_rules.scalars().first():
            for rule_type, params in DEFAULT_RULES:
                db.add(
                    RiskRule(
                        user_id=user.id,
                        environment=Environment.PAPER.value,
                        rule_type=rule_type.value,
                        params=params,
                    )
                )
            print(f"created {len(DEFAULT_RULES)} default risk rules")

        existing_strategy = await db.execute(select(Strategy).where(Strategy.user_id == user.id))
        if not existing_strategy.scalars().first():
            db.add(
                Strategy(
                    user_id=user.id,
                    broker_account_id=account.id,
                    name="Demo SMA Crossover",
                    kind="sma_crossover",
                    environment=Environment.PAPER.value,
                    symbols=["RELIANCE", "TCS"],
                    params={"fast": 5, "slow": 20, "quantity": 10, "exchange": "NSE", "product": "MIS"},
                    status=StrategyStatus.DRAFT.value,
                )
            )
            print("created demo strategy (DRAFT — start it from the UI)")

        existing_caps = await db.execute(select(BrokerCapability).limit(1))
        if not existing_caps.scalars().first():
            for broker, caps in CAPABILITY_MATRIX.items():
                for field in (
                    "place_order", "modify_order", "cancel_order", "holdings", "positions",
                    "funds", "instruments_dump", "websocket_ticks", "order_postbacks",
                    "amo_orders", "bracket_gtt",
                ):
                    db.add(
                        BrokerCapability(
                            broker=broker.value,
                            capability=field,
                            supported=getattr(caps, field),
                            notes=caps.notes,
                        )
                    )
            print("seeded broker capability matrix")

        await db.commit()
        print("seed complete")


if __name__ == "__main__":
    asyncio.run(seed())
