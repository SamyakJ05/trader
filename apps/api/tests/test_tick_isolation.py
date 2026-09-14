"""One bad account must not stop the platform.

paper_tick runs every five seconds for every user on the instance. Before
these guards, a single account tripping a settlement invariant propagated out
of the loop and stopped ticks, fills and marks for everyone -- and kept doing
so on every subsequent tick, since the same row was still broken.
"""

import uuid
from types import SimpleNamespace

from app.engines.paper import settlement


class FakeResult:
    def __init__(self, rows=()):
        self._rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows), __iter__=lambda s: iter(self._rows))


class FakeDb:
    def __init__(self, account_ids):
        self._account_ids = account_ids
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, *args, **kwargs):
        return FakeResult(self._account_ids)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


async def test_one_failing_account_does_not_stop_the_others(monkeypatch):
    good_a, bad, good_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    settled = []

    async def fake_settle_account(db, account_id, now):
        if account_id == bad:
            raise settlement.SettlementError("invariant violated on this account")
        settled.append(account_id)

    monkeypatch.setattr(settlement, "settle_account", fake_settle_account)
    db = FakeDb([good_a, bad, good_b])

    count = await settlement.settle_due(db)

    assert settled == [good_a, good_b], "healthy accounts must still settle"
    assert count == 2
    assert db.rollbacks == 1, "the failure is rolled back on its own"


async def test_a_failure_does_not_discard_earlier_successes(monkeypatch):
    """Committing per account is what keeps one bad row from undoing the work
    already done for everybody before it."""
    first, broken = uuid.uuid4(), uuid.uuid4()

    async def fake_settle_account(db, account_id, now):
        if account_id == broken:
            raise settlement.SettlementError("boom")

    monkeypatch.setattr(settlement, "settle_account", fake_settle_account)
    db = FakeDb([first, broken])

    await settlement.settle_due(db)

    assert db.commits >= 1, "the healthy account's settlement was committed"


async def test_settle_due_reports_how_many_succeeded(monkeypatch):
    ids = [uuid.uuid4() for _ in range(3)]

    async def fake_settle_account(db, account_id, now):
        return None

    monkeypatch.setattr(settlement, "settle_account", fake_settle_account)
    assert await settlement.settle_due(FakeDb(ids)) == 3
