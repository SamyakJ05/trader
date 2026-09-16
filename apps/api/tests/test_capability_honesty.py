"""Capability flags describe OUR adapter, not the broker's API.

Groww's block declared place_order, modify_order, cancel_order,
instruments_dump and websocket_ticks all True while every one of those
methods raised FeatureNotSupportedError immediately -- its own notes field
even said "Feed/streaming not implemented" three lines below the flag
claiming otherwise. Anything gating on these would have offered Groww as a
tradable broker that throws on every order.

This walks the matrix and checks each trading flag against what the adapter
class actually does, so a flag cannot drift from the code again.
"""

import inspect

import pytest

from app.adapters.groww.adapter import GrowwAdapter
from app.adapters.icici_breeze.adapter import BreezeAdapter
from app.adapters.paper.adapter import PaperAdapter
from app.adapters.zerodha.adapter import ZerodhaAdapter
from app.domain.capabilities import CAPABILITY_MATRIX
from app.domain.enums import Broker

_ADAPTER_CLASSES = {
    Broker.PAPER: PaperAdapter,
    Broker.ZERODHA: ZerodhaAdapter,
    Broker.GROWW: GrowwAdapter,
    Broker.ICICI_BREEZE: BreezeAdapter,
}


def _refuses_unconditionally(cls, method_name: str) -> bool:
    """True when the method's body does nothing but raise.

    A method that raises after real work (a broker rejection, a missing
    session) is implemented; one whose entire body is a raise is a stub, and
    a capability flag saying otherwise is false.
    """
    method = getattr(cls, method_name, None)
    if method is None:
        return True
    try:
        source = inspect.getsource(method)
    except (OSError, TypeError):  # pragma: no cover
        return False
    body = [
        line.strip()
        for line in source.splitlines()[1:]
        if line.strip() and not line.strip().startswith("#")
    ]
    # Drop the docstring if there is one.
    if body and body[0].startswith(('"""', "'''")):
        closing = next(
            (i for i, line in enumerate(body[1:], 1) if line.endswith(('"""', "'''"))),
            0,
        )
        body = body[closing + 1 :]
    return bool(body) and body[0].startswith("raise ")


# websocket_ticks is checked against tick_feed, not subscribe_ticks: Zerodha
# and Breeze both refuse subscribe_ticks on purpose, because the base
# interface hands back a bare iterator while a socket connection has a
# lifetime someone must own and close. Both document that and expose
# tick_feed instead, so the flag is honest and the stub is deliberate --
# checking the wrong method here reported two false positives on the first
# run of this test.
@pytest.mark.parametrize(
    "flag,method",
    [
        ("place_order", "place_order"),
        ("modify_order", "modify_order"),
        ("cancel_order", "cancel_order"),
        ("instruments_dump", "get_instruments"),
        ("websocket_ticks", "tick_feed"),
    ],
)
@pytest.mark.parametrize("broker", list(CAPABILITY_MATRIX))
def test_a_capability_flag_is_not_claimed_by_a_stub(broker, flag, method):
    capabilities = CAPABILITY_MATRIX[broker]
    if not getattr(capabilities, flag):
        return  # Claiming nothing is always honest.
    assert not _refuses_unconditionally(_ADAPTER_CLASSES[broker], method), (
        f"{broker.value} declares {flag}=True but {method}() only raises. "
        "A flag that promises what the adapter refuses is worse than one "
        "that admits the gap."
    )
