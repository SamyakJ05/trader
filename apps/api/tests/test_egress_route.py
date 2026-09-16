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
    """The cache is module-global, so a test that leaves an entry behind
    changes the next test's answer. This failed in CI and not locally purely
    on test ordering, which is the kind of shared state worth removing rather
    than working around."""
    system._reset_egress_cache()
    yield
    system._reset_egress_cache()


def patch(monkeypatch, *, detected, expected, live=False):
    """`expected` may be a single address or a comma-separated pair, matching
    what ICICI register (a Primary and a Secondary)."""
    # Reset inside the patch too, not only in the fixture. The cache is
    # module-global: whether a previous test left an entry depends on
    # collection order, which differs between a local run and CI -- and that
    # difference is exactly what made this suite pass here and fail there.
    system._reset_egress_cache()

    async def fake_detect(*a, **kw):
        return detected

    monkeypatch.setattr(system, "detect_egress_ip", fake_detect)
    monkeypatch.setattr(
        system,
        "get_settings",
        lambda: SimpleNamespace(
            broker_static_ip=expected,
            broker_static_ips=(
                [p.strip() for p in expected.split(",") if p.strip()] if expected else []
            ),
            enable_live_trading=live,
        ),
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

    system._reset_egress_cache()
    monkeypatch.setattr(system, "detect_egress_ip", counting)
    monkeypatch.setattr(
        system,
        "get_settings",
        lambda: SimpleNamespace(
            broker_static_ip="1.2.3.4",
            broker_static_ips=["1.2.3.4"],
            enable_live_trading=False,
        ),
    )
    for _ in range(5):
        await system.egress_ip(user=None)
    assert len(calls) == 1


def test_the_route_paths_match_what_the_frontend_calls():
    """system.py declares full paths per route rather than using a router
    prefix, so a new route that omits /system is mounted somewhere the UI does
    not call. That is a 404 at runtime and nothing catches it -- /egress-ip
    shipped that way and the banner would never have loaded.
    """
    paths = {r.path for r in system.router.routes}
    assert "/system/egress-ip" in paths
    # The paper reset genuinely has no /system prefix; the UI must match it
    # rather than assume one.
    assert "/paper/accounts/{account_id}/reset" in paths


# ── two registered addresses ─────────────────────────────────────────


async def test_either_registered_address_counts_as_a_match(monkeypatch):
    """ICICI whitelist a Primary and a Secondary. Which one an order leaves
    from is a property of the host's routing -- a droplet with a reserved IP
    receives on one address and egresses from another -- so both must pass."""
    patch(monkeypatch, detected="206.189.128.192",
          expected="68.183.244.153, 206.189.128.192")
    assert (await system.egress_ip(user=None))["status"] == "match"

    system._reset_egress_cache()
    patch(monkeypatch, detected="68.183.244.153",
          expected="68.183.244.153, 206.189.128.192")
    assert (await system.egress_ip(user=None))["status"] == "match"


async def test_an_address_on_neither_slot_is_still_a_mismatch(monkeypatch):
    """Accepting a list must not become accepting anything."""
    patch(monkeypatch, detected="9.9.9.9",
          expected="68.183.244.153, 206.189.128.192")
    result = await system.egress_ip(user=None)
    assert result["status"] == "mismatch"
    # The message names both, so the operator can see which slots are filled.
    assert "68.183.244.153" in result["expected"]
    assert "206.189.128.192" in result["expected"]
