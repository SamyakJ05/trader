"""Worker liveness.

The arq worker is what advances paper fills and runs strategies. When it dies
the api stays up and every page keeps rendering — positions simply stop moving,
and orders sit unfilled. Nothing in the system notices, which makes this the
failure most worth detecting and the one least likely to be seen.

So the worker stamps a key on every tick and the key carries a TTL a few
multiples longer than the tick interval. Liveness is then the key's existence:
if the worker stops, Redis expires the key on its own and /readyz starts
failing without anything having to run a check. Expiry doing the work means
there is no monitoring process that can itself die silently.
"""

import time

from app.core.logging import get_logger

logger = get_logger(__name__)

HEARTBEAT_KEY = "worker:heartbeat"

# paper_tick runs every 5s. Three missed ticks is a worker that is genuinely
# gone rather than one briefly busy or blocked on a slow query, and 20s is
# short enough that a monitor polling each minute still catches it.
HEARTBEAT_TTL_SECONDS = 20


async def beat(redis) -> None:
    """Record that the worker is alive. Called from the fastest cron job."""
    await redis.set(HEARTBEAT_KEY, str(int(time.time())), ex=HEARTBEAT_TTL_SECONDS)


async def last_beat(redis) -> int | None:
    """Unix seconds of the last tick, or None if the worker is not beating."""
    raw = await redis.get(HEARTBEAT_KEY)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):  # pragma: no cover - corrupt value
        return None


async def is_alive(redis) -> bool:
    return await last_beat(redis) is not None
