"""The triple live-gate is the platform's core safety claim:
live orders must be structurally impossible until every gate opens."""

from types import SimpleNamespace

import pytest

import app.services.orders as orders_module
from app.domain.capabilities import CAPABILITY_MATRIX
from app.domain.enums import AdapterStatus, Broker
from app.services.orders import _live_gate


def account(broker="zerodha", live_enabled=False):
    return SimpleNamespace(broker=broker, live_enabled=live_enabled)


def patch_settings(monkeypatch, enable_live_trading: bool):
    monkeypatch.setattr(
        orders_module,
        "get_settings",
        lambda: SimpleNamespace(enable_live_trading=enable_live_trading),
    )


def test_gate1_global_flag_blocks(monkeypatch):
    patch_settings(monkeypatch, enable_live_trading=False)
    reason = _live_gate(account(live_enabled=True))
    assert reason is not None and "ENABLE_LIVE_TRADING" in reason


def test_gate2_account_flag_blocks(monkeypatch):
    patch_settings(monkeypatch, enable_live_trading=True)
    reason = _live_gate(account(live_enabled=False))
    assert reason is not None and "live_enabled" in reason


def test_gate3_adapter_status_blocks(monkeypatch):
    patch_settings(monkeypatch, enable_live_trading=True)
    reason = _live_gate(account(broker="zerodha", live_enabled=True))
    assert reason is not None and "adapter status" in reason


@pytest.mark.parametrize(
    "broker",
    [b for b in CAPABILITY_MATRIX if b != Broker.PAPER],
)
def test_no_real_broker_passes_the_gate_today(monkeypatch, broker):
    """Regression for the 'live trading is structurally impossible' claim.
    If an adapter is ever flipped to WORKING, this test fails on purpose so
    the flip is a deliberate, reviewed act."""
    patch_settings(monkeypatch, enable_live_trading=True)
    assert CAPABILITY_MATRIX[broker].adapter_status != AdapterStatus.WORKING
    assert _live_gate(account(broker=broker.value, live_enabled=True)) is not None


# ── the gate has to be REACHABLE, not just correct ──────────────────────
# Every test above checks _live_gate() in isolation, and all of them passed
# while order dispatch never reached it. A real broker account sits in paper
# environment for the whole verification playbook (stages 1-4 say so
# explicitly); dispatch branched on environment, took the simulated path,
# and then called get_adapter() -- which dispatches on BROKER -- so the
# order went to the real broker anyway and the paper engine booked a
# fabricated fill for it. Nothing in a gate-only test can catch that.


def routing_account(broker: str, environment: str):
    return SimpleNamespace(
        broker=broker, environment=environment, credential_ref=None, live_enabled=False
    )


@pytest.mark.parametrize("broker", [b.value for b in CAPABILITY_MATRIX])
def test_paper_environment_never_dispatches_through_a_real_broker(monkeypatch, broker):
    """The bug this file previously could not see: whatever the broker, an
    account in paper environment must place orders through the simulator."""
    from app.adapters.paper.adapter import PaperAdapter
    from app.adapters.registry import get_trading_adapter

    monkeypatch.setenv("BREEZE_MAIN_API_KEY", "k")
    monkeypatch.setenv("BREEZE_MAIN_API_SECRET", "s")
    adapter = get_trading_adapter(routing_account(broker, "paper"))
    assert isinstance(adapter, PaperAdapter), (
        f"{broker} in paper environment dispatched through a real adapter -- "
        "a live order with a simulated fill booked against it"
    )


@pytest.mark.parametrize(
    "broker",
    [b.value for b in CAPABILITY_MATRIX if b != Broker.PAPER],
)
def test_live_environment_dispatches_through_the_real_broker(monkeypatch, broker):
    """The mirror: paper routing must not swallow a genuinely live order and
    silently simulate it. Live environment reaches the real adapter, where
    _live_gate then applies."""
    from app.adapters.paper.adapter import PaperAdapter
    from app.adapters.registry import get_trading_adapter

    for ref in ("ZERODHA_MAIN", "GROWW_MAIN", "BREEZE_MAIN"):
        monkeypatch.setenv(f"{ref}_API_KEY", "k")
        monkeypatch.setenv(f"{ref}_API_SECRET", "s")
    adapter = get_trading_adapter(routing_account(broker, "live"))
    assert not isinstance(adapter, PaperAdapter)


def test_read_paths_still_reach_the_real_broker_in_paper_environment(monkeypatch):
    """get_adapter must NOT gain the paper-environment behaviour: the
    playbook's early stages pull a real account's profile, funds, holdings
    and security master while trading stays simulated. Routing those through
    the simulator would have broken the verification flow entirely."""
    from app.adapters.icici_breeze.adapter import BreezeAdapter
    from app.adapters.registry import get_adapter

    monkeypatch.setenv("BREEZE_MAIN_API_KEY", "k")
    monkeypatch.setenv("BREEZE_MAIN_API_SECRET", "s")
    account = routing_account("icici_breeze", "paper")
    account.credential_ref = "BREEZE_MAIN"
    assert isinstance(get_adapter(account), BreezeAdapter)
