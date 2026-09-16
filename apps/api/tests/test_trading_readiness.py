"""The one check the daily routine depends on.

Breeze sessions die at midnight IST and ICICI publish no way to renew one
programmatically, so the operator logs in each morning before the open. After
that, nobody is watching -- strategies trade unattended for the session. So
"is it actually ready?" has to be answerable in one place, and every failing
check has to say what to do about it.
"""

import uuid
from types import SimpleNamespace

from app.services import brokers as broker_service


def account(**kw):
    base = dict(
        id=uuid.uuid4(), user_id=uuid.uuid4(), broker="icici_breeze",
        environment="live", status="connected", live_enabled=True,
        session_expires_at=None, label="ICICI",
    )
    base.update(kw)
    return SimpleNamespace(**base)


class FakeDb:
    """Serves the readiness queries in order: accounts, then strategies."""

    def __init__(self, accounts=(), strategies=()):
        self._queue = [list(accounts), list(strategies)]

    async def execute(self, *a, **kw):
        rows = self._queue.pop(0) if self._queue else []

        class Result:
            def scalars(inner):
                return SimpleNamespace(all=lambda: rows, __iter__=lambda s: iter(rows))

        return Result()


def setup(monkeypatch, *, live_gate=True, engaged=False, verified=True, coverage=None):
    from app.domain.enums import AdapterStatus

    monkeypatch.setattr(
        broker_service, "get_settings",
        lambda: SimpleNamespace(enable_live_trading=live_gate),
    )
    monkeypatch.setattr(
        broker_service, "get_capabilities",
        lambda broker: SimpleNamespace(
            adapter_status=AdapterStatus.WORKING if verified else AdapterStatus.SCAFFOLD
        ),
    )

    async def fake_engaged(redis):
        return engaged

    from app.services import killswitch

    monkeypatch.setattr(killswitch, "is_global_engaged", fake_engaged)
    monkeypatch.setattr(broker_service, "get_redis", lambda: None, raising=False)

    async def fake_coverage(db, account):
        return coverage or {"symbols": 0, "resolved": 0, "missing": []}

    monkeypatch.setattr(broker_service, "_instrument_coverage", fake_coverage)


def find(report, name):
    return next(c for c in report["checks"] if c["check"] == name)


# ── the happy path ───────────────────────────────────────────────────


async def test_everything_configured_reports_ready(monkeypatch):
    setup(monkeypatch)
    db = FakeDb(accounts=[account()], strategies=[SimpleNamespace(name="s")])
    report = await broker_service.trading_readiness(db, uuid.uuid4())
    failing = [f"{c['check']}: {c['detail']}" for c in report["checks"] if not c["ok"]]
    assert report["ready"] is True, f"unexpected failures: {failing}"


# ── each thing that stops trading ────────────────────────────────────


async def test_no_account_stops_immediately(monkeypatch):
    """Nothing else matters, so the report does not go on listing checks the
    operator cannot act on yet."""
    setup(monkeypatch)
    report = await broker_service.trading_readiness(FakeDb(), uuid.uuid4())
    assert report["ready"] is False
    assert len(report["checks"]) == 1
    assert find(report, "broker account")["fix"]


async def test_an_expired_session_is_named_as_the_daily_step(monkeypatch):
    """The one thing that fails every single day. The fix must say so, or the
    operator reads it as a fault rather than the routine."""
    setup(monkeypatch)
    db = FakeDb(accounts=[account(status="session_expired")], strategies=[])
    report = await broker_service.trading_readiness(db, uuid.uuid4())
    check = find(report, "broker session")
    assert check["ok"] is False
    assert "daily" in check["fix"]


async def test_the_global_gate_being_off_is_reported(monkeypatch):
    setup(monkeypatch, live_gate=False)
    db = FakeDb(accounts=[account()], strategies=[SimpleNamespace(name="s")])
    report = await broker_service.trading_readiness(db, uuid.uuid4())
    assert find(report, "live trading gate")["ok"] is False
    assert report["ready"] is False


async def test_an_unverified_adapter_is_reported(monkeypatch):
    """The state today: no order has ever been sent to a real broker, so every
    live order is refused. The fix names what has to happen first."""
    setup(monkeypatch, verified=False)
    db = FakeDb(accounts=[account()], strategies=[SimpleNamespace(name="s")])
    report = await broker_service.trading_readiness(db, uuid.uuid4())
    check = find(report, "adapter verified")
    assert check["ok"] is False
    assert "one real order" in check["fix"]


async def test_an_engaged_kill_switch_is_reported(monkeypatch):
    """Silent by design and the easiest thing to leave on overnight."""
    setup(monkeypatch, engaged=True)
    db = FakeDb(accounts=[account()], strategies=[SimpleNamespace(name="s")])
    report = await broker_service.trading_readiness(db, uuid.uuid4())
    assert find(report, "kill switch")["ok"] is False


async def test_no_running_strategy_is_reported(monkeypatch):
    setup(monkeypatch)
    db = FakeDb(accounts=[account()], strategies=[])
    report = await broker_service.trading_readiness(db, uuid.uuid4())
    assert find(report, "running strategies")["ok"] is False


async def test_symbols_without_tokens_are_named(monkeypatch):
    """A strategy with no ticks cannot trade even when every other check
    passes: the runner refuses to act without a fresh quote, and nothing
    errors -- it simply never trades."""
    setup(monkeypatch, coverage={"symbols": 3, "resolved": 1, "missing": ["INFTEC", "TCS"]})
    db = FakeDb(accounts=[account()], strategies=[SimpleNamespace(name="s")])
    report = await broker_service.trading_readiness(db, uuid.uuid4())
    check = find(report, "instrument tokens")
    assert check["ok"] is False
    assert "INFTEC" in check["fix"]


# ── the report's own contract ────────────────────────────────────────


async def test_every_failing_check_says_what_to_do(monkeypatch):
    """A readiness report that says "not ready" without a next step is a
    worse version of the dashboard the operator already had."""
    setup(monkeypatch, live_gate=False, engaged=True, verified=False,
          coverage={"symbols": 2, "resolved": 0, "missing": ["A", "B"]})
    db = FakeDb(accounts=[account(status="session_expired")], strategies=[])
    report = await broker_service.trading_readiness(db, uuid.uuid4())
    failing = [c for c in report["checks"] if not c["ok"]]
    assert failing, "expected failures in this configuration"
    assert all(c["fix"] for c in failing)
