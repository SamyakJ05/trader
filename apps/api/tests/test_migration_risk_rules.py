"""Migration 0012 against a database that actually has users.

This shipped broken. The INSERT omitted created_at, which is NOT NULL with its
default declared on the ORM model (default=utcnow) -- Python-side only, so raw
SQL bypasses it and Postgres refuses the row.

CI passed anyway, because its database has no users: the INSERT matched zero
rows and succeeded trivially. The failure only appeared on a deploy to a real
database, mid-upgrade, leaving the API container down.

So this runs the migration's OWN statement, read from the migration module,
against a database with a user in it. An earlier version of this test
hand-copied the SQL with bind parameters and spent two CI rounds failing on
its own binds while proving nothing about the migration -- a test that
rewrites what it tests is not testing it.
"""

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="requires disposable Postgres"
)


def _migration_statements() -> list[str]:
    """The exact SQL 0012 runs, captured by calling its upgrade() with a
    recording op.

    Reading the statements rather than reimplementing them is the point: the
    bug was IN the statement, so a test that writes its own version cannot
    catch it.
    """
    import importlib.util
    from pathlib import Path
    from unittest.mock import patch

    path = (
        Path(__file__).resolve().parents[1]
        / "alembic" / "versions" / "0012_default_risk_rules.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0012", path)
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
async def user_id(sessions):
    """A real user, so the migration's INSERT has a row to write -- which is
    exactly the condition CI lacked when this bug shipped."""
    async with sessions() as db:
        created = (
            await db.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, is_active, "
                    "is_admin, created_at) VALUES (gen_random_uuid(), :e, 'x', "
                    "true, false, now()) RETURNING id"
                ),
                {"e": f"{uuid.uuid4()}@migration-test.local"},
            )
        ).scalar_one()
        await db.commit()
    yield created
    async with sessions() as db:
        await db.execute(text("DELETE FROM users WHERE id = :u"), {"u": created})
        await db.commit()


async def test_the_backfill_inserts_for_a_real_user(sessions, user_id):
    """Every NOT NULL column has to be satisfied, created_at included."""
    async with sessions() as db:
        for statement in _migration_statements():
            await db.execute(text(statement))
        await db.commit()

    async with sessions() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT rule_type, environment, created_at FROM risk_rules "
                    "WHERE user_id = :u"
                ),
                {"u": user_id},
            )
        ).all()

    assert rows, "the migration inserted nothing for an existing user"
    assert all(r.created_at is not None for r in rows), "created_at was not populated"
    # Both environments, because an account can be switched between them and a
    # limit that exists in only one silently disappears on the switch.
    assert {r.environment for r in rows} == {"paper", "live"}
    assert "MAX_TOTAL_EXPOSURE" in {r.rule_type for r in rows}


async def test_the_backfill_is_idempotent(sessions, user_id):
    """Deploys get re-run, and a second insert would leave a user with two
    conflicting limits of the same type."""
    statements = _migration_statements()
    for _ in range(2):
        async with sessions() as db:
            for statement in statements:
                await db.execute(text(statement))
            await db.commit()

    async with sessions() as db:
        count = (
            await db.execute(
                text(
                    "SELECT count(*) FROM risk_rules WHERE user_id = :u "
                    "AND rule_type = 'MAX_TOTAL_EXPOSURE' AND environment = 'live'"
                ),
                {"u": user_id},
            )
        ).scalar_one()
    assert count == 1
