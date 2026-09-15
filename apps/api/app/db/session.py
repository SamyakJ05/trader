from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()

# Managed Postgres caps connections, and the cap is per cluster rather than per
# process. DigitalOcean's smallest plan allows ~22, and this application opens
# pools from several processes at once: each uvicorn worker has its own, and so
# does the arq worker. Left at SQLAlchemy's defaults (5 + 10 overflow) three
# processes can ask for 45 and the cluster starts refusing connections — which
# surfaces as orders failing, not as an obvious database error.
#
# So the budget is explicit and deliberately conservative. With the default 3
# and 2, two api workers plus one arq worker reach 15 at saturation, leaving
# headroom for migrations, a psql session and the platform's own connections.
# Raise DB_POOL_SIZE only alongside the plan's connection limit.
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,          # managed Postgres closes idle connections
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_recycle=settings.db_pool_recycle_seconds,
    pool_timeout=settings.db_pool_timeout_seconds,
)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        yield session
