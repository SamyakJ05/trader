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
