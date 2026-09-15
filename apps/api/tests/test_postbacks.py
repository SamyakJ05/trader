"""Kite order postbacks.

The postback URL is public. Anything reaching it is untrusted until its
checksum verifies, because a forged postback that marked an order FILLED would
have the platform book a position it does not hold — and then trade against it.

Postbacks also repeat and arrive out of order, so reconciliation has to be
idempotent and forward-only.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.domain.enums import OrderStatus
from app.services import postbacks

SECRET = "kite-api-secret"


def payload(**overrides):
    body = {
        "user_id": "AB1234",
        "order_id": "250915000001",
        "order_timestamp": "2026-09-15 10:30:00",
        "status": "COMPLETE",
        "filled_quantity": 10,
        "average_price": 2845.5,
    }
    body.update(overrides)
    body.setdefault(
        "checksum",
        postbacks.expected_checksum(body["order_id"], body["order_timestamp"], SECRET),
    )
    return body


def order_row(status=OrderStatus.OPEN, broker_order_id="250915000001"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status.value,
        broker_order_id=broker_order_id,
        client_order_id="cli-1",
        filled_quantity=0,
        average_fill_price=None,
        status_message=None,
    )


def account_row(environment="paper"):
    """Defaults to paper: these tests exercise status reconciliation, and fill
    booking is live-only (covered in test_live_fills.py)."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        broker="zerodha",
        broker_client_id="AB1234",
        credential_ref="ZERODHA_MAIN",
        environment=environment,
    )


class FakeResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeDb:
    def __init__(self, *results):
        self._results = list(results)
        self.added = []

    async def execute(self, *args, **kwargs):
        return FakeResult(self._results.pop(0) if self._results else None)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        pass


# ── checksum ─────────────────────────────────────────────────────────


def test_a_genuine_checksum_verifies():
    assert postbacks.verify_checksum(payload(), SECRET)


def test_a_forged_checksum_is_refused():
    """The whole point: without this, anyone who can POST to the public URL
    can mark any order filled."""
    assert not postbacks.verify_checksum(payload(checksum="deadbeef"), SECRET)


def test_a_checksum_from_a_different_secret_is_refused():
    forged = payload()
    forged["checksum"] = postbacks.expected_checksum(
        forged["order_id"], forged["order_timestamp"], "someone-elses-secret"
    )
    assert not postbacks.verify_checksum(forged, SECRET)


def test_tampering_with_the_order_id_invalidates_the_checksum():
    """A replayed postback pointed at a different order must not verify."""
    body = payload()
    body["order_id"] = "999999999999"
    assert not postbacks.verify_checksum(body, SECRET)


def test_tampering_with_the_timestamp_invalidates_the_checksum():
    body = payload()
    body["order_timestamp"] = "2026-09-15 11:00:00"
    assert not postbacks.verify_checksum(body, SECRET)


@pytest.mark.parametrize("missing", ["checksum", "order_id", "order_timestamp"])
def test_a_payload_missing_a_signed_field_is_refused(missing):
    body = payload()
    body.pop(missing)
    assert not postbacks.verify_checksum(body, SECRET)


def test_no_secret_means_no_verification():
    """An account with no configured secret cannot authenticate anything, so
    everything is refused rather than waved through."""
    assert not postbacks.verify_checksum(payload(), "")


# ── resolving the account ────────────────────────────────────────────


async def test_a_postback_for_an_unknown_account_is_refused():
    with pytest.raises(postbacks.PostbackError, match="No broker account"):
        await postbacks.verify(FakeDb(None), payload())


async def test_a_postback_without_a_user_id_is_refused():
    with pytest.raises(postbacks.PostbackError):
        await postbacks.verify(FakeDb(None), payload(user_id=None))


async def test_verification_fails_without_a_configured_secret(monkeypatch):
    monkeypatch.delenv("ZERODHA_MAIN_API_SECRET", raising=False)
    with pytest.raises(postbacks.PostbackError, match="api secret"):
        await postbacks.verify(FakeDb(account_row()), payload())


async def test_a_verified_postback_returns_its_account(monkeypatch):
    monkeypatch.setenv("ZERODHA_MAIN_API_SECRET", SECRET)
    account = account_row()
    assert await postbacks.verify(FakeDb(account), payload()) is account


async def test_a_forged_postback_is_refused_at_verification(monkeypatch):
    monkeypatch.setenv("ZERODHA_MAIN_API_SECRET", SECRET)
    with pytest.raises(postbacks.PostbackError, match="Checksum"):
        await postbacks.verify(FakeDb(account_row()), payload(checksum="forged"))


# ── reconciliation ───────────────────────────────────────────────────


async def test_a_completed_order_is_marked_filled():
    order = order_row(status=OrderStatus.OPEN)
    result = await postbacks.reconcile(FakeDb(order), account_row(), payload())
    assert result is order
    assert order.status == OrderStatus.FILLED.value
    assert order.filled_quantity == 10


async def test_a_terminal_order_is_never_reopened():
    """Postbacks arrive out of order. A stale OPEN after a fill must not undo
    it — the platform would think it still had a live order."""
    order = order_row(status=OrderStatus.FILLED)
    result = await postbacks.reconcile(
        FakeDb(order), account_row(), payload(status="OPEN")
    )
    assert result is None
    assert order.status == OrderStatus.FILLED.value


async def test_a_repeated_postback_is_a_no_op():
    """Kite retries. Applying the same state twice must not emit a second
    state-change event."""
    order = order_row(status=OrderStatus.OPEN)
    assert await postbacks.reconcile(
        FakeDb(order), account_row(), payload(status="OPEN", filled_quantity=0)
    ) is None


async def test_a_partly_filled_open_order_is_recorded_as_partial():
    """Kite reports a partly-filled live order as OPEN with a quantity.
    Recording it as plain OPEN would lose the fill."""
    order = order_row(status=OrderStatus.ACCEPTED)
    await postbacks.reconcile(
        FakeDb(order), account_row(), payload(status="OPEN", filled_quantity=4)
    )
    assert order.status == OrderStatus.PARTIALLY_FILLED.value
    assert order.filled_quantity == 4


async def test_a_rejection_carries_its_reason():
    order = order_row(status=OrderStatus.OPEN)
    await postbacks.reconcile(
        FakeDb(order),
        account_row(),
        payload(status="REJECTED", status_message="Insufficient funds"),
    )
    assert order.status == OrderStatus.REJECTED.value
    assert "Insufficient funds" in order.status_message


async def test_an_unknown_broker_status_leaves_the_order_alone():
    """A state we do not understand must not be guessed into one we do."""
    order = order_row(status=OrderStatus.OPEN)
    assert await postbacks.reconcile(
        FakeDb(order), account_row(), payload(status="SOME NEW STATE")
    ) is None
    assert order.status == OrderStatus.OPEN.value


async def test_a_postback_for_an_order_we_never_placed_is_ignored():
    """An order placed directly in Kite, outside the platform. Not an error —
    the raw event is still stored by the route."""
    assert await postbacks.reconcile(FakeDb(None), account_row(), payload()) is None


async def test_a_postback_without_an_order_id_is_ignored():
    assert await postbacks.reconcile(
        FakeDb(order_row()), account_row(), payload(order_id=None)
    ) is None


async def test_a_malformed_average_price_does_not_break_reconciliation():
    """Bad data in one optional field must not lose the state change itself."""
    order = order_row(status=OrderStatus.OPEN)
    await postbacks.reconcile(
        FakeDb(order), account_row(), payload(average_price="not-a-number")
    )
    assert order.status == OrderStatus.FILLED.value


async def test_handle_verifies_before_reconciling(monkeypatch):
    """A forged postback must never reach the order row at all."""
    monkeypatch.setenv("ZERODHA_MAIN_API_SECRET", SECRET)
    order = order_row(status=OrderStatus.OPEN)
    with pytest.raises(postbacks.PostbackError):
        await postbacks.handle(
            FakeDb(account_row(), order), payload(checksum="forged")
        )
    assert order.status == OrderStatus.OPEN.value, "a forgery must not move an order"
