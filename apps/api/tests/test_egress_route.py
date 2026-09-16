"""Reporting the outbound IP to the operator.

SEBI's algo framework requires order requests to originate from an IP the
broker has whitelisted. Orders from anywhere else are rejected while reads,
market data and the websocket keep working -- so the platform looks healthy
and only trading is broken, and the rejection does not say why. This was
visible only in a startup log line or a hand-typed docker exec.
"""

from types import SimpleNamespace

import pytest

from app.api.routes import system


@pytest.fixture(autouse=True)
def clear_cache():
    system._egress_cache.update({"at": 0.0, "ip": None})
    yield
    system._egress_cache.update({"at": 0.0, "ip": None})


def patch(monkeypatch, *, detected, expected, live=False):
    async def fake_detect(*a, **kw):
        return detected

    monkeypatch.setattr(system, "detect_egress_ip", fake_detect)
    monkeypatch.setattr(
        system,
        "get_settings",
        lambda: SimpleNamespace(broker_static_ip=expected, enable_live_trading=live),
    )


async def test_a_matching_address_is_reported_as_a_match(monkeypatch):
    patch(monkeypatch, detected="1.2.3.4", expected="1.2.3.4")
    result = await system.egress_ip(user=None)
    assert result["status"] == "match"


async def test_a_changed_address_is_a_mismatch(monkeypatch):
    """The case this exists for: a droplet rebuild or a detached reserved IP
    silently breaks live trading while everything else keeps working."""
    patch(monkeypatch, detected="5.6.7.8", expected="1.2.3.4")
    result = await system.egress_ip(user=None)
    assert result["status"] == "mismatch"
    assert result["detected"] == "5.6.7.8"
    assert result["expected"] == "1.2.3.4"


async def test_an_unreachable_echo_service_is_unknown_not_a_mismatch(monkeypatch):
    """Says nothing about whether the address is right. A red banner over
    someone else's outage would train the operator to ignore it."""
    patch(monkeypatch, detected=None, expected="1.2.3.4")
    result = await system.egress_ip(user=None)
    assert result["status"] == "unknown"


async def test_no_registered_address_is_unconfigured(monkeypatch):
    """Paper-only deployments never register one, and nagging them about a
    setting they do not need is noise."""
    patch(monkeypatch, detected="1.2.3.4", expected=None)
    result = await system.egress_ip(user=None)
    assert result["status"] == "unconfigured"


async def test_the_lookup_is_cached(monkeypatch):
    """The lookup leaves the host to a third-party echo service. A UI polling
    this must not repeat it on every render."""
    calls = []

    async def counting(*a, **kw):
        calls.append(1)
        return "1.2.3.4"

    monkeypatch.setattr(system, "detect_egress_ip", counting)
    monkeypatch.setattr(
        system,
        "get_settings",
        lambda: SimpleNamespace(broker_static_ip="1.2.3.4", enable_live_trading=False),
    )
    for _ in range(5):
        await system.egress_ip(user=None)
    assert len(calls) == 1
