from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import get_settings
from app.core.egress import report_egress_ip
from app.core.preflight import check_production_config
from app.core.logging import configure_logging, get_logger
from app.core.redis import close_redis

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    logger.info(
        "api_starting",
        live_trading=settings.enable_live_trading,
        market_hours_enforced=settings.market_hours_enforced,
    )
    # Settings whose wrong value is otherwise silent. Logged, not raised:
    # refusing to boot would take down a running instance over what may be a
    # deliberate choice.
    check_production_config(settings)
    # Diagnostic only: a mismatch with the broker's whitelist is logged, never
    # enforced here. See app/core/egress.py.
    await report_egress_ip(settings.broker_static_ip)
    yield
    await close_redis()


app = FastAPI(
    title="Trader API",
    description="Broker-agnostic algorithmic trading platform for India. "
    "Paper trading is the default; live trading is gated per broker.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
