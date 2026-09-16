import time
import uuid

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.deps import DbSession, VerifiedUser
from app.core.egress import detect_egress_ip
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.db.models import Strategy
from app.domain.enums import Broker, StrategyStatus
from app.engines.paper import engine as paper_engine
from app.services import brokers as broker_service
from app.services import heartbeat, killswitch

logger = get_logger(__name__)

router = APIRouter(tags=["system"])


@router.get("/healthz")
async def healthz(db: DbSession, response: Response):
    """Liveness of this process and its dependencies.

    Returns 503 when degraded, not 200 with a body saying so: uptime monitors
    and container orchestrators read the status code, and a 200 here would
    report the instance healthy while Postgres was unreachable.
    """
    checks = {"db": False, "redis": False}
    try:
        await db.execute(text("SELECT 1"))
        checks["db"] = True
    except Exception:
        logger.warning("healthz_db_unreachable", exc_info=True)
    try:
        checks["redis"] = await get_redis().ping()
    except Exception:
        logger.warning("healthz_redis_unreachable", exc_info=True)
    healthy = all(checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "degraded", "checks": checks}


@router.get("/readyz")
async def readyz(db: DbSession, response: Response):
    """Everything healthz covers, plus whether the worker is still ticking.

    The arq worker advances paper fills and runs strategies. When it dies the
    api stays up and every page keeps rendering -- positions simply stop
    moving. That is the failure least likely to be noticed, so it gets an
    endpoint an uptime monitor can watch.

    Kept separate from /healthz because the two answer different questions: a
    container orchestrator restarting the api because the worker died would be
    the wrong response to the right signal.
    """
    redis = get_redis()
    checks = {"db": False, "redis": False, "worker": False}
    try:
        await db.execute(text("SELECT 1"))
        checks["db"] = True
    except Exception:
        logger.warning("readyz_db_unreachable", exc_info=True)
    try:
        checks["redis"] = await redis.ping()
    except Exception:
        logger.warning("readyz_redis_unreachable", exc_info=True)

    last = None
    if checks["redis"]:
        # Redis expiring the key is what makes the worker's death visible, so
        # this check is only meaningful when Redis itself is reachable.
        try:
            last = await heartbeat.last_beat(redis)
            checks["worker"] = last is not None
        except Exception:
            logger.warning("readyz_heartbeat_failed", exc_info=True)

    ready = all(checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    body = {"status": "ok" if ready else "degraded", "checks": checks}
    if last is not None:
        body["worker_last_beat_age_seconds"] = max(0, int(time.time()) - last)
    return body


@router.get("/system/config")
async def system_config(user: VerifiedUser):
    settings = get_settings()
    return {
        "live_trading_enabled": settings.enable_live_trading,
        "market_hours_enforced": settings.market_hours_enforced,
        # The UI hides paper affordances when this is off. Reported rather
        # than inferred from the account list: an instance with no paper
        # accounts left is not the same as one that refuses to make them.
        "paper_trading_enabled": settings.enable_paper_trading,
    }


class KillSwitchBody(BaseModel):
    scope: str  # "global" | "strategy"
    engaged: bool
    strategy_id: uuid.UUID | None = None
    reason: str = ""


async def _owned_strategy_ids(db: DbSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    result = await db.execute(select(Strategy.id).where(Strategy.user_id == user_id))
    return set(result.scalars())


@router.get("/system/killswitch")
async def killswitch_status(user: VerifiedUser, db: DbSession):
    return await killswitch.status(
        get_redis(), owned_strategy_ids=await _owned_strategy_ids(db, user.id)
    )


@router.post("/system/killswitch")
async def set_killswitch(body: KillSwitchBody, user: VerifiedUser, db: DbSession):
    redis = get_redis()
    if body.scope == "global":
        # The global switch halts strategy execution for EVERY user on this
        # instance — an operator break-glass, not a per-user control.
        if not user.is_admin:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "The global kill switch is an operator action; "
                "use a per-strategy kill switch to stop your own strategies",
            )
        await killswitch.set_global(
            redis, db, engaged=body.engaged, user_id=user.id, reason=body.reason
        )
        if body.engaged:
            # Engaging globally stops running strategies hard, for every user
            # -- not only the operator's own. The Redis flag already halts new
            # orders instance-wide, so scoping this to the engager left every
            # other user's strategies recorded as RUNNING while they were not,
            # and resumed all of them at once on disengage.
            from sqlalchemy import update

            await db.execute(
                update(Strategy)
                .where(Strategy.status == StrategyStatus.RUNNING.value)
                .values(status=StrategyStatus.KILLED.value)
            )
    elif body.scope == "strategy":
        if body.strategy_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "strategy_id required")
        # Ownership is resolved BEFORE the switch is written: engaging a kill
        # switch on a foreign strategy halts it on every runner tick.
        result = await db.execute(
            select(Strategy).where(
                Strategy.id == body.strategy_id, Strategy.user_id == user.id
            )
        )
        strategy = result.scalar_one_or_none()
        if strategy is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
        await killswitch.set_strategy(
            redis,
            db,
            strategy_id=body.strategy_id,
            engaged=body.engaged,
            user_id=user.id,
            reason=body.reason,
        )
        if body.engaged and strategy.status == StrategyStatus.RUNNING.value:
            strategy.status = StrategyStatus.KILLED.value
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "scope must be global|strategy")
    await db.commit()
    return await killswitch.status(
        redis, owned_strategy_ids=await _owned_strategy_ids(db, user.id)
    )


@router.post("/paper/accounts/{account_id}/reset")
async def reset_paper_account(account_id: uuid.UUID, user: VerifiedUser, db: DbSession):
    account = await broker_service.get_account(db, user.id, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    # broker, not environment. reset_account deletes every Position and
    # PaperHolding row for the account and rewrites its cash ledger -- fine
    # for the simulator, destructive and irreversible for a real broker
    # account, which sits in paper environment for the whole of the
    # verification playbook and would have been accepted here.
    if account.broker != Broker.PAPER.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only the paper simulator can be reset — this is a real broker account",
        )
    await paper_engine.reset_account(db, account)
    await db.commit()
    return {"status": "reset", "cash": str(paper_engine.INITIAL_PAPER_CASH)}


# ── egress IP ────────────────────────────────────────────────────────

# The lookup leaves the host to a third-party echo service, so a UI that polls
# this must not repeat it on every render. The address changes only when the
# host does -- a droplet rebuild, a reserved IP detaching, a NAT change -- so
# a few minutes of staleness costs nothing and a cache miss is the only path
# that touches the network.
_EGRESS_TTL_SECONDS = 300
_egress_cache: dict = {"at": None, "ip": None}


def _reset_egress_cache() -> None:
    """Drop the cached lookup. Used by tests, which must not depend on
    whichever test ran before them having left it empty."""
    _egress_cache.update({"at": None, "ip": None})


@router.get("/system/egress-ip")
async def egress_ip(user: VerifiedUser):
    """This host's outbound address, and whether it matches the registered one.

    SEBI's algo framework requires order requests to originate from an IP the
    broker has whitelisted. Orders from anywhere else are rejected while reads,
    market data and the websocket keep working perfectly -- so the platform
    looks healthy and only trading is broken, and the rejection does not say
    why. Suspicion falls on the session or the payload first, which is where
    the hours go.

    Reported here so a mismatch after a droplet rebuild is visible before
    market open rather than discovered by a failed order during it.
    """
    # `at` is None until the first lookup. Using 0.0 as the sentinel made a
    # populated cache indistinguishable from an empty one after any reset,
    # which is how a stale None survived into the next caller.
    now = time.monotonic()
    cached_at = _egress_cache["at"]
    if cached_at is None or now - cached_at > _EGRESS_TTL_SECONDS:
        _egress_cache["ip"] = await detect_egress_ip()
        _egress_cache["at"] = now

    detected = _egress_cache["ip"]
    expected = get_settings().broker_static_ip

    if not expected:
        status_value = "unconfigured"
    elif detected is None:
        # The echo services are unreachable, which says nothing about whether
        # the address is right. Reported as unknown rather than as a mismatch:
        # a red banner over someone else's outage would train the operator to
        # ignore it.
        status_value = "unknown"
    elif detected == expected:
        status_value = "match"
    else:
        status_value = "mismatch"

    return {
        "detected": detected,
        "expected": expected,
        "status": status_value,
        "live_trading_enabled": get_settings().enable_live_trading,
    }
