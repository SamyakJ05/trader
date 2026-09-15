"""Who may be reset.

paper_engine.reset_account deletes every Position and PaperHolding row for
an account and rewrites its cash ledger. That is the right thing for the
simulator and irreversible destruction for a real broker account.

The guard used to read `account.environment != "paper"`, which accepts a
real broker account for the entire verification playbook -- stages 1 to 4
leave a real account in paper environment by design. This pins the guard to
the broker instead, and pins the mirror case too: the simulator must stay
resettable whatever environment it is in.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.domain.enums import Broker


def account(broker: str, environment: str):
    return SimpleNamespace(broker=broker, environment=environment)


def guard(acct) -> None:
    """The condition as system.py:reset_paper_account applies it.

    Restated here rather than imported: the check sits inline in an async
    route that needs a DB session and an authenticated user, and this suite
    has no fixture for either (the Postgres-backed tests skip without a
    database). So this pins the RULE, not the call site -- it would not catch
    someone editing system.py to key on environment again. Worth knowing
    rather than assuming; a route-level test is the real coverage and does
    not exist yet.
    """
    if acct.broker != Broker.PAPER.value:
        raise HTTPException(409, "Only the paper simulator can be reset")


@pytest.mark.parametrize("environment", ["paper", "live"])
def test_the_simulator_is_resettable_in_either_environment(environment):
    guard(account("paper", environment))


@pytest.mark.parametrize("broker", ["icici_breeze", "zerodha", "groww"])
@pytest.mark.parametrize("environment", ["paper", "live"])
def test_a_real_broker_account_is_never_resettable(broker, environment):
    """Including paper environment -- the state this instance's real ICICI
    account is in right now, and the one the old guard let through."""
    with pytest.raises(HTTPException) as exc:
        guard(account(broker, environment))
    assert exc.value.status_code == 409
