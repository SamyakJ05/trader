"""A real broker account must not be created stamped 'paper'.

environment defaults to paper and the UI has no control for it, so the default
alone produced an ICICI Breeze account -- holding real stock -- recorded as
paper. The label was the least of it: the strategy runner skips a non-live
strategy on a live-only instance and leaves it RUNNING, so it looks started
and never trades.

Migration 0013 corrects the accounts that already exist. This covers the next
one created.
"""

import pytest

from app.api.routes import brokers as broker_routes
from app.domain.enums import Broker, Environment


class _Settings:
    def __init__(self, paper: bool):
        self.enable_paper_trading = paper


def _resolved(broker: Broker, requested: Environment, *, paper_enabled: bool) -> str:
    """The environment create_account would store, with nothing else running.

    The route body is read rather than executed: calling it needs a database
    session, a user and an audit sink, none of which participate in this
    decision. What is pinned is the rule, not the plumbing around it.
    """
    environment = requested
    if broker != Broker.PAPER and not paper_enabled:
        environment = Environment.LIVE
    return environment.value


@pytest.mark.parametrize("requested", [Environment.PAPER, Environment.LIVE])
def test_real_broker_is_live_on_a_live_only_instance(requested):
    """Whatever was asked for -- including the default that caused this."""
    assert _resolved(Broker.ICICI_BREEZE, requested, paper_enabled=False) == "live"


def test_paper_broker_is_never_promoted():
    """The paper broker has no real account behind it; promoting it would
    point the runner at a broker that cannot place an order."""
    assert _resolved(Broker.PAPER, Environment.PAPER, paper_enabled=False) == "paper"


def test_an_instance_with_paper_enabled_still_honours_the_request():
    """The override exists because paper is disabled, so it must not fire on
    an instance where paper trading is a legitimate choice."""
    assert _resolved(Broker.ICICI_BREEZE, Environment.PAPER, paper_enabled=True) == "paper"


def test_the_route_still_contains_the_override():
    """The rule above is a restatement, so it would keep passing if the route
    lost the override. This fails if the two drift apart."""
    import inspect

    source = inspect.getsource(broker_routes.create_account)
    assert "environment = Environment.LIVE" in source
    assert "enable_paper_trading" in source
