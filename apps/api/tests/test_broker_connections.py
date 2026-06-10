"""Connection-surface tests: masked credentials, live-enable gate,
Zerodha connect initiation, dashboard aggregation. Pure-unit (no DB)."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import app.services.brokers as broker_service_module
from app.adapters.base import BrokerError
from app.adapters.zerodha.adapter import ZerodhaAdapter
from app.api.routes.brokers import _account_out
from app.api.routes.dashboard import account_aggregates
from app.core.config import BrokerEnvCredentials
from app.services.brokers import can_enable_live, credential_status


def account_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        broker="zerodha",
        label="Main",
        environment="paper",
        status="disconnected",
        status_message=None,
        live_enabled=False,
        last_sync_at=None,
        read_verified_at=None,
        credential_ref="ZERODHA_TEST",
        broker_client_id=None,
        session_token_enc=None,
        session_expires_at=None,
        user_id=uuid.uuid4(),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ── masked credentials ──────────────────────────────────────────────


def test_credential_status_never_leaks_secrets(monkeypatch):
    monkeypatch.setenv("ZERODHA_TEST_API_KEY", "super-secret-key")
    monkeypatch.setenv("ZERODHA_TEST_API_SECRET", "super-secret-secret")
    account = account_row(session_token_enc="enc:abc123token")

    status = credential_status(account)
    assert status["env_keys_configured"] is True
    assert status["session_token_configured"] is True
    serialized = str(status)
    assert "super-secret" not in serialized
    assert "abc123token" not in serialized


def test_account_out_contains_no_secret_material(monkeypatch):
    monkeypatch.setenv("ZERODHA_TEST_API_KEY", "kkk-secret")
    monkeypatch.setenv("ZERODHA_TEST_API_SECRET", "sss-secret")
    account = account_row(session_token_enc="enc:raw-token-material")

    out = _account_out(account).model_dump()
    dumped = str(out)
    assert "kkk-secret" not in dumped
    assert "sss-secret" not in dumped
    assert "raw-token-material" not in dumped
    assert out["adapter_status"] == "scaffold"  # honesty embedded in the DTO
    assert out["credentials"]["session_token_configured"] is True


# ── live-enable gate mirrors the order pipeline ─────────────────────


def patch_settings(monkeypatch, enable_live: bool):
    monkeypatch.setattr(
        broker_service_module,
        "get_settings",
        lambda: SimpleNamespace(enable_live_trading=enable_live),
    )


def test_live_enable_refuses_paper(monkeypatch):
    patch_settings(monkeypatch, True)
    assert "Paper" in can_enable_live(account_row(broker="paper"))


def test_live_enable_refuses_when_global_gate_closed(monkeypatch):
    patch_settings(monkeypatch, False)
    assert "ENABLE_LIVE_TRADING" in can_enable_live(account_row())


def test_live_enable_refuses_scaffold_adapter(monkeypatch):
    patch_settings(monkeypatch, True)
    reason = can_enable_live(
        account_row(read_verified_at=datetime.now(timezone.utc))
    )
    assert reason is not None and "scaffold" in reason


def test_live_enable_requires_read_verification(monkeypatch):
    # Even if an adapter were flipped to working, unverified read access blocks.
    patch_settings(monkeypatch, True)
    reason = can_enable_live(account_row(read_verified_at=None))
    assert reason is not None


# ── Zerodha connect initiation ──────────────────────────────────────


def make_adapter(monkeypatch, with_key: bool) -> ZerodhaAdapter:
    if with_key:
        monkeypatch.setenv("ZERODHA_TEST_API_KEY", "test_api_key")
    else:
        monkeypatch.delenv("ZERODHA_TEST_API_KEY", raising=False)
    return ZerodhaAdapter(account_row(), BrokerEnvCredentials("ZERODHA_TEST"))


async def test_zerodha_connect_returns_login_url(monkeypatch):
    adapter = make_adapter(monkeypatch, with_key=True)
    result = await adapter.connect()
    assert result["flow"] == "redirect"
    assert result["login_url"].startswith("https://kite.zerodha.com/connect/login")
    assert "api_key=test_api_key" in result["login_url"]


async def test_zerodha_connect_without_key_fails_loudly(monkeypatch):
    adapter = make_adapter(monkeypatch, with_key=False)
    with pytest.raises(BrokerError):
        await adapter.connect()


# ── dashboard aggregation ───────────────────────────────────────────


def test_account_aggregates_counts_and_honesty_lists():
    synced = account_row(
        broker="paper",
        status="connected",
        last_sync_at=datetime.now(timezone.utc),
        read_verified_at=datetime.now(timezone.utc),
    )
    stale = account_row(broker="zerodha", environment="live", live_enabled=True)
    agg = account_aggregates([synced, stale])

    assert agg["total"] == 2
    assert agg["connected"] == 1
    assert agg["by_broker"] == {"paper": 1, "zerodha": 1}
    assert agg["by_environment"] == {"paper": 1, "live": 1}
    assert agg["live_configured"] == 1
    assert agg["never_synced"] == [str(stale.id)]
    assert agg["read_unverified"] == [str(stale.id)]
