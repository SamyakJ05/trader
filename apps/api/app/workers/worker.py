"""arq worker. Run with: arq app.workers.worker.WorkerSettings"""

from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.workers.jobs import paper_tick, strategy_tick


async def startup(ctx: dict) -> None:
    configure_logging()


class WorkerSettings:
    functions: list = []
    cron_jobs = [
        # price step + fills every 5s; strategies every 15s
        cron(paper_tick, second={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        cron(strategy_tick, second={0, 15, 30, 45}),
    ]
    on_startup = startup
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
