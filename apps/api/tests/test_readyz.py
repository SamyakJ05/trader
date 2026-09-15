"""Health and readiness endpoints.

The status code is the contract: uptime monitors and orchestrators read it,
not the body. A 200 carrying {"status": "degraded"} reports the instance
healthy to everything that matters.
"""

import pytest
from fakeredis import FakeAsyncRedis
from fastapi import Response

from app.api.routes import system
from app.services import heartbeat


class _DB:
    """Minimal stand-in: these endpoints only ever issue SELECT 1."""

    def __init__(self, ok=True):
        self._ok = ok

    async def execute(self, *args, **kwargs):
        if not self._ok:
            raise RuntimeError("database unreachable")


@pytest.fixture
def redis(monkeypatch):
    fake = FakeAsyncRedis(decode_responses=True)
    monkeypatch.setattr(system, "get_redis", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_healthz_ok(redis):
    response = Response()
    body = await system.healthz(_DB(), response)
    assert body["status"] == "ok"
    assert response.status_code != 503


@pytest.mark.asyncio
async def test_healthz_is_503_when_db_is_down(redis):
    response = Response()
    body = await system.healthz(_DB(ok=False), response)
    assert body["status"] == "degraded"
    assert body["checks"]["db"] is False
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_readyz_needs_a_beating_worker(redis):
    """Everything up except the worker is still not ready -- that is the
    whole point of the endpoint."""
    response = Response()
    body = await system.readyz(_DB(), response)
    assert body["checks"]["db"] is True
    assert body["checks"]["worker"] is False
    assert body["status"] == "degraded"
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_readyz_ok_with_a_live_worker(redis):
    await heartbeat.beat(redis)
    response = Response()
    body = await system.readyz(_DB(), response)
    assert body["status"] == "ok"
    assert body["checks"]["worker"] is True
    assert body["worker_last_beat_age_seconds"] >= 0
    assert response.status_code != 503


@pytest.mark.asyncio
async def test_readyz_reports_db_failure_alongside_a_live_worker(redis):
    await heartbeat.beat(redis)
    response = Response()
    body = await system.readyz(_DB(ok=False), response)
    assert body["checks"]["worker"] is True
    assert body["checks"]["db"] is False
    assert response.status_code == 503
