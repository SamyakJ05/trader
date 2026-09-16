"""Migration 0013 against a database that actually has accounts.

Runs the migration's OWN statements, captured from the module, against real
rows -- the lesson from 0012, which passed a CI whose database had nothing for
the statement to match and then failed on a real upgrade.

What is being protected is not a label. A broker account stamped 'paper' makes
the strategy runner skip every strategy on it while leaving them RUNNING, so a
live account looks started and silently never trades.
"""

import importlib.util
import os
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="requires disposable Postgres"
)


def _migration_statements() -> list[str]:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic" / "versions" / "0013_stamp_real_broker_accounts_live.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0013", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    captured: list[str] = []
    with patch.object(module.op, "execute", side_effect=captured.append):
        module.upgrade()
    return captured


@pytest.fixture
async def sessions():
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def accounts(sessions):
    """One real-broker account and one paper account, both stamped 'paper'.

    The paper one is the control: the migration must leave it alone, or
    re-enabling paper trading would find its accounts silently converted to
    live and placing real orders.
    """
    async with sessions() as db:
        user = (
            await db.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, is_active, "
                    "is_admin, created_at) VALUES (gen_random_uuid(), :e, 'x', "
                    "true, false, now()) RETURNING id"
                ),
                {"e": f"{uuid.uuid4()}@migration-test.local"},
            )
        ).scalar_one()
        made = {}
        for key, broker in (("real", "icici_breeze"), ("paper", "paper")):
            made[key] = (
                await db.execute(
                    text(
                        "INSERT INTO broker_accounts (id, user_id, broker, label, "
                        "environment, status, created_at) VALUES "
                        "(gen_random_uuid(), :u, :b, :l, 'paper', 'connected', now()) "
                        "RETURNING id"
                    ),
                    {"u": user, "b": broker, "l": f"{key}-{uuid.uuid4().hex[:6]}"},
                )
            ).scalar_one()
        await db.commit()
    yield made
    async with sessions() as db:
        await db.execute(text("DELETE FROM users WHERE id = :u"), {"u": user})
        await db.commit()


async def test_real_account_becomes_live_and_paper_is_left_alone(sessions, accounts):
    async with sessions() as db:
        for statement in _migration_statements():
            await db.execute(text(statement))
        await db.commit()

    async with sessions() as db:
        got = dict(
            (
                await db.execute(
                    text(
                        "SELECT id, environment FROM broker_accounts "
                        "WHERE id = ANY(:ids)"
                    ),
                    {"ids": list(accounts.values())},
                )
            ).all()
        )

    assert got[accounts["real"]] == "live", (
        "a real broker account left stamped paper makes the runner skip every "
        "strategy on it while leaving them RUNNING"
    )
    assert got[accounts["paper"]] == "paper", "a paper account must stay paper"
