from fastapi import APIRouter

from app.api.routes import (
    ai,
    audit_log,
    auth,
    brokers,
    dashboard,
    orders,
    portfolio,
    risk,
    strategies,
    system,
    webhooks,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(brokers.router)
api_router.include_router(dashboard.router)
api_router.include_router(portfolio.router)
api_router.include_router(orders.router)
api_router.include_router(strategies.router)
api_router.include_router(ai.router)
api_router.include_router(risk.router)
api_router.include_router(audit_log.router)
api_router.include_router(webhooks.router)
api_router.include_router(system.router)
