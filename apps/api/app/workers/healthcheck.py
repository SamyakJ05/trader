"""Worker liveness probe, for the container healthcheck.

Exits 0 while the worker is beating and 1 otherwise. A module rather than a
python -c one-liner so the quoting lives in Python instead of inside YAML
inside a shell, and so a Redis outage exits non-zero quietly rather than
printing a traceback into the container logs every thirty seconds.
"""

import asyncio
import sys


async def _alive() -> bool:
    from app.core.redis import get_redis
    from app.services.heartbeat import is_alive

    try:
        return await is_alive(get_redis())
    except Exception:
        # Redis being unreachable is not the worker being dead, but from a
        # healthcheck's position the two are indistinguishable and both want
        # attention. Report unhealthy without the noise.
        return False


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(_alive()) else 1)
