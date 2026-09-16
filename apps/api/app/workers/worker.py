"""arq worker. Run with: arq app.workers.worker.WorkerSettings"""

import asyncio
from contextlib import suppress

from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.workers import tick_stream
from app.workers.jobs import (
    broker_session_tick,
    instrument_sync_tick,
    paper_tick,
    strategy_tick,
)


async def startup(ctx: dict) -> None:
    configure_logging()
    # The live tick stream is a long-lived socket, not a periodic job, so it
    # cannot be a cron entry -- but it had no caller of any kind, which meant
    # no live tick was ever ingested by a running process. Everything
    # downstream depends on it: the Redis quote cache that every live order is
    # risk-checked against, and the candles live strategies read. Without it a
    # live order is rejected for want of a fresh quote before it ever reaches
    # the live gate.
    #
    # It lives in the arq worker rather than the API's lifespan because the
    # API runs multiple uvicorn workers: one stream per process would open
    # duplicate sockets and subscribe each instrument several times over. The
    # arq worker is a single process, so exactly one stream exists per account.
    ctx["tick_stream"] = asyncio.create_task(tick_stream.run_forever())


async def shutdown(ctx: dict) -> None:
    """Close the tick sockets before the process exits.

    Without this the task is killed mid-frame and Breeze sees a half-closed
    socket rather than a disconnect, which delays its own cleanup of the
    session's subscriptions.
    """
    task = ctx.get("tick_stream")
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


class WorkerSettings:
    functions: list = []
    cron_jobs = [
        # price step + fills every 5s; strategies every 15s
        cron(paper_tick, second={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        cron(strategy_tick, second={0, 15, 30, 45}),
        # Broker sessions die at the daily exchange flush (~6am IST). Checked
        # every quarter hour rather than once at the flush, so a worker that
        # was down at 6am still corrects itself rather than leaving every
        # account claiming to be connected all day.
        cron(broker_session_tick, minute={0, 15, 30, 45}),
        # 03:00 UTC is 08:30 IST: after Breeze regenerates its security
        # master (~08:00 IST) and Kite refreshes theirs, and before the
        # market opens. arq has no timezone parameter -- it fires on the
        # container clock, which is UTC here -- so this is written in UTC
        # deliberately rather than looking like a mistimed 3am job.
        #
        # run_at_startup because the instrument master is a hard dependency
        # of the tick stream and of strategy creation: without it a fresh
        # deployment would have an empty table until the next morning, and
        # every symbol would be rejected as unknown in the meantime.
        cron(instrument_sync_tick, hour={3}, minute={0}, run_at_startup=True),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
