"""The egress diagnostic must report, and must never block startup."""

import httpx
import pytest
from structlog.testing import capture_logs

from app.core import egress


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self._handler = handler

    async def handle_async_request(self, request):
        return self._handler(request)


@pytest.fixture
def patch_client(monkeypatch):
    def apply(handler):
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = _Transport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(egress.httpx, "AsyncClient", factory)

    return apply


@pytest.mark.asyncio
async def test_detects_address(patch_client):
    patch_client(lambda request: httpx.Response(200, text="203.0.113.7\n"))
    assert await egress.detect_egress_ip() == "203.0.113.7"


@pytest.mark.asyncio
async def test_falls_back_to_second_provider(patch_client):
    def handler(request):
        if "ipify" in str(request.url):
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, text="198.51.100.4")

    patch_client(handler)
    assert await egress.detect_egress_ip() == "198.51.100.4"


@pytest.mark.asyncio
async def test_unknown_when_every_provider_fails(patch_client):
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    patch_client(handler)
    assert await egress.detect_egress_ip() is None
    # And reporting still returns rather than raising: a third-party outage
    # must not stop the api from starting.
    assert await egress.report_egress_ip("203.0.113.7") is None


@pytest.mark.asyncio
async def test_mismatch_is_logged_as_an_error(patch_client):
    """structlog renders to its own stream, so assert on the captured events
    rather than on caplog, which sees nothing."""
    patch_client(lambda request: httpx.Response(200, text="203.0.113.7"))
    with capture_logs() as events:
        await egress.report_egress_ip(expected="198.51.100.4")
    mismatch = [e for e in events if e["event"] == "egress_ip_mismatch"]
    assert len(mismatch) == 1
    assert mismatch[0]["log_level"] == "error"
    assert mismatch[0]["detected"] == "203.0.113.7"
    assert mismatch[0]["registered"] == "198.51.100.4"


@pytest.mark.asyncio
async def test_match_is_not_an_error(patch_client):
    patch_client(lambda request: httpx.Response(200, text="203.0.113.7"))
    with capture_logs() as events:
        await egress.report_egress_ip(expected="203.0.113.7")
    assert not [e for e in events if e["event"] == "egress_ip_mismatch"]
    assert [e for e in events if e["event"] == "egress_ip"]


@pytest.mark.asyncio
async def test_reports_without_a_registered_address(patch_client):
    """Before registration there is nothing to compare against, and the
    detected address is exactly what the form needs."""
    patch_client(lambda request: httpx.Response(200, text="203.0.113.7"))
    assert await egress.report_egress_ip(expected=None) == "203.0.113.7"
