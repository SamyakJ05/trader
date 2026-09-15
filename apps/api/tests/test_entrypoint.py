"""Which peer uvicorn trusts for X-Forwarded-For.

The client IP is the axis the login and password-reset rate limiters use to
stop one source sweeping many accounts. Trusting the wrong peer does not fail
loudly — the limits go on looking enforced while resting on a header anyone
can forge — so the fail-closed behaviour is pinned here.
"""

import importlib

import pytest

from app import entrypoint


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("PROXY_IP", raising=False)
    monkeypatch.delenv("PROXY_HOST", raising=False)
    importlib.reload(entrypoint)
    yield
    importlib.reload(entrypoint)


def test_explicit_proxy_ip_wins(monkeypatch):
    monkeypatch.setenv("PROXY_IP", "10.0.0.5")
    assert entrypoint.proxy_addresses() == ["10.0.0.5"]


def test_several_addresses_are_accepted(monkeypatch):
    monkeypatch.setenv("PROXY_IP", "10.0.0.5, 10.0.0.6")
    assert entrypoint.proxy_addresses() == ["10.0.0.5", "10.0.0.6"]


def test_hostname_is_resolved(monkeypatch):
    monkeypatch.setenv("PROXY_HOST", "localhost")
    importlib.reload(entrypoint)
    assert "127.0.0.1" in entrypoint.proxy_addresses()


def test_unresolvable_proxy_yields_nothing(monkeypatch):
    monkeypatch.setenv("PROXY_HOST", "no-such-proxy.invalid")
    importlib.reload(entrypoint)
    assert entrypoint.proxy_addresses() == []


def test_startup_refuses_rather_than_trusting_everyone(monkeypatch):
    """The failure that matters: no resolvable proxy must not become '*'."""
    monkeypatch.setenv("PROXY_HOST", "no-such-proxy.invalid")
    importlib.reload(entrypoint)

    def fail(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("uvicorn was started without a trusted proxy")

    monkeypatch.setattr(entrypoint.os, "execvp", fail)
    with pytest.raises(SystemExit) as exc:
        entrypoint.main()
    assert exc.value.code == 1


def test_startup_passes_only_the_trusted_addresses(monkeypatch):
    monkeypatch.setenv("PROXY_IP", "10.0.0.5")
    importlib.reload(entrypoint)
    captured = {}

    def capture(binary, argv):
        captured["argv"] = argv

    monkeypatch.setattr(entrypoint.os, "execvp", capture)
    entrypoint.main()
    argv = captured["argv"]
    assert "--proxy-headers" in argv
    assert argv[argv.index("--forwarded-allow-ips") + 1] == "10.0.0.5"
    assert "*" not in argv
