"""Dashboard aggregate endpoint. One call feeds the dashboard page.

Honest partial-data semantics: accounts that have never synced or verified
are listed as such instead of being silently folded into totals."""

from collections import Counter
from decimal import Decimal
from types import SimpleNamespace

from fastapi import APIRouter
from sqlalchemy import func, select

from app.core.deps import DbSession, VerifiedUser
from app.core.redis import get_redis
from app.db.models import (
    AuditEvent,
    BrokerAccount,
    FundsSnapshot,
    HoldingsSnapshot,
    Order,
    PaperHolding,
    Position,
    Strategy,
)
from app.domain.enums import Broker, OrderStatus
from app.engines.paper import ledger
from app.services import killswitch

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_OPEN_ORDER_STATUSES = [
    OrderStatus.ACCEPTED.value,
    OrderStatus.SUBMITTED.value,
    OrderStatus.OPEN.value,
    OrderStatus.PARTIALLY_FILLED.value,
]


def account_aggregates(accounts: list[BrokerAccount]) -> dict:
    """Pure aggregation over account rows — counts plus honesty lists."""
    by_broker = Counter(a.broker for a in accounts)
    by_environment = Counter(a.environment for a in accounts)
    return {
        "total": len(accounts),
        "connected": sum(1 for a in accounts if a.status == "connected"),
        "by_broker": dict(by_broker),
        "by_environment": dict(by_environment),
        "live_configured": sum(1 for a in accounts if a.live_enabled),
        "never_synced": [str(a.id) for a in accounts if a.last_sync_at is None],
        "read_unverified": [str(a.id) for a in accounts if a.read_verified_at is None],
    }


async def _latest_snapshot(db, model, account_id):
    result = await db.execute(
        select(model)
        .where(model.broker_account_id == account_id)
        .order_by(model.ts.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


@router.get("/summary")
async def dashboard_summary(user: VerifiedUser, db: DbSession):
    accounts = (
        (
            await db.execute(
                select(BrokerAccount)
                .where(BrokerAccount.user_id == user.id)
                .order_by(BrokerAccount.created_at)
            )
        )
        .scalars()
        .all()
    )

    per_account = []
    funds_total = Decimal("0")
    funds_known = True
    for a in accounts:
        # broker == "paper" is the platform's own simulator, an entirely
        # separate signal from environment == "paper" (a real broker account
        # can sit in paper environment throughout Breeze/Zerodha verification
        # and still hold real synced data). Branching on environment here
        # routed a real, correctly-synced Breeze account through
        # ledger.get_cash() instead of its own FundsSnapshot. It happened to
        # show the right number anyway right now -- get_cash() itself falls
        # back to the latest FundsSnapshot when no CashLedger rows exist yet
        # (confirmed: this account has none) -- but that fallback breaks the
        # moment any paper trade is placed on this account, at which point it
        # would silently start showing stale ledger data instead of a real
        # broker's actual funds. Reading the real function before asserting
        # this the first time would have caught the wrong initial claim here.
        if a.broker == Broker.PAPER.value:
            entry = await ledger.latest(db, a.id)
            funds = SimpleNamespace(
                available_cash=await ledger.get_cash(db, a.id),
                ts=entry.ts if entry else a.created_at,
            )
            rows = (
                (
                    await db.execute(
                        select(PaperHolding).where(
                            PaperHolding.broker_account_id == a.id,
                            PaperHolding.quantity > 0,
                        )
                    )
                )
                .scalars()
                .all()
            )
            holdings = SimpleNamespace(holdings=rows)
        else:
            funds = await _latest_snapshot(db, FundsSnapshot, a.id)
            holdings = await _latest_snapshot(db, HoldingsSnapshot, a.id)
        if funds is not None:
            funds_total += funds.available_cash
        else:
            funds_known = False
        per_account.append(
            {
                "id": str(a.id),
                "broker": a.broker,
                "label": a.label,
                "environment": a.environment,
                "status": a.status,
                "last_sync_at": a.last_sync_at.isoformat() if a.last_sync_at else None,
                "read_verified_at": (
                    a.read_verified_at.isoformat() if a.read_verified_at else None
                ),
                "available_cash": str(funds.available_cash) if funds else None,
                "funds_as_of": funds.ts.isoformat() if funds else None,
                "holdings_count": len(holdings.holdings) if holdings else None,
            }
        )

    open_positions = (
        await db.execute(
            select(func.count())
            .select_from(Position)
            .where(Position.user_id == user.id, Position.quantity != 0)
        )
    ).scalar_one()

    open_orders = (
        await db.execute(
            select(func.count())
            .select_from(Order)
            .where(Order.user_id == user.id, Order.status.in_(_OPEN_ORDER_STATUSES))
        )
    ).scalar_one()

    strategy_rows = (
        await db.execute(
            select(Strategy.status, func.count())
            .where(Strategy.user_id == user.id)
            .group_by(Strategy.status)
        )
    ).all()

    recent_events = (
        (
            await db.execute(
                select(AuditEvent)
                .where(AuditEvent.user_id == user.id)
                .order_by(AuditEvent.id.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )

    return {
        "accounts": account_aggregates(list(accounts)),
        "per_account": per_account,
        "funds": {
            # Partial-data honesty: total only claimed when every account
            # has at least one funds snapshot.
            "total_available_cash": str(funds_total),
            "complete": funds_known,
        },
        "open_positions": open_positions,
        "open_orders": open_orders,
        "strategies": {status: count for status, count in strategy_rows},
        "killswitch": await killswitch.status(
            get_redis(),
            owned_strategy_ids=set(
                (await db.execute(select(Strategy.id).where(Strategy.user_id == user.id))).scalars()
            ),
        ),
        "recent_events": [
            {
                "id": e.id,
                "ts": e.ts.isoformat(),
                "event_type": e.event_type,
                "entity_type": e.entity_type,
                "entity_id": e.entity_id,
                "payload": e.payload,
            }
            for e in recent_events
        ],
    }
