"""Migration 0012 against a database that actually has users.

This shipped broken. The INSERT omitted created_at, which is NOT NULL with its
default declared on the ORM model (default=utcnow) -- Python-side only, so raw
SQL bypasses it and Postgres refuses the row.

CI passed anyway, because its database has no users: the INSERT matched zero
rows and succeeded trivially. The failure only appeared on a deploy to a real
database, mid-upgrade, leaving the API container down.

So the test that matters is not "does the migration run" but "does it run when
there is a user for it to insert against".
"""

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="requires disposable Postgres"
)

# The statement from alembic/versions/0012_default_risk_rules.py, verbatim in
# shape. Duplicated rather than imported because a migration is a historical
# artefact: it must keep working as written, even after the service it mirrors
# changes.
#
# CAST(:params AS jsonb) rather than :params::jsonb -- SQLAlchemy cannot tell
# a bind parameter from Postgres's :: cast operator, and leaves the parameter
# unbound. The migration itself inlines its literals and has no binds, so this
# difference is the test's alone.
_INSERT = """
INSERT INTO risk_rules
    (id, user_id, environment, rule_type, params, enabled, created_at)
SELECT gen_random_uuid(), u.id, :env, :rule, CAST(:params AS jsonb), true, now()
FROM users u
WHERE NOT EXISTS (
    SELECT 1 FROM risk_rules r
    WHERE r.user_id = u.id AND r.environment = :env AND r.rule_type = :rule
)
"""


@pytest.fixture
async def sessions():
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_the_backfill_inserts_for_a_real_user(sessions):
    """The case CI never exercised: a user exists, so the INSERT has a row to
    write and every NOT NULL column has to be satisfied."""
    async with sessions() as db:
        email = f"{uuid.uuid4()}@migration-test.local"
        user_id = (
            await db.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, is_active, "
                    "is_admin, created_at) VALUES (gen_random_uuid(), :e, 'x', "
                    "true, false, now()) RETURNING id"
                ),
                {"e": email},
            )
        ).scalar_one()

        await db.execute(
            text(_INSERT),
            {"env": "live", "rule": "MAX_TOTAL_EXPOSURE",
             "params": '{"max_exposure": 100000}'},
        )

        row = (
            await db.execute(
                text(
                    "SELECT params, created_at FROM risk_rules "
                    "WHERE user_id = :u AND rule_type = 'MAX_TOTAL_EXPOSURE'"
                ),
                {"u": user_id},
            )
        ).first()
        assert row is not None, "no rule was inserted for an existing user"
        assert row.created_at is not None, "created_at was not populated"
        assert row.params == {"max_exposure": 100000}

        await db.rollback()


async def test_the_backfill_is_idempotent(sessions):
    """Deploys get re-run, and a second insert of the same rule would leave a
    user with two conflicting limits of the same type."""
    async with sessions() as db:
        email = f"{uuid.uuid4()}@migration-test.local"
        user_id = (
            await db.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, is_active, "
                    "is_admin, created_at) VALUES (gen_random_uuid(), :e, 'x', "
                    "true, false, now()) RETURNING id"
                ),
                {"e": email},
            )
        ).scalar_one()

        params = {"env": "live", "rule": "MAX_DAILY_TURNOVER",
                  "params": '{"max_turnover": 200000}'}
        await db.execute(text(_INSERT), params)
        await db.execute(text(_INSERT), params)

        count = (
            await db.execute(
                text(
                    "SELECT count(*) FROM risk_rules WHERE user_id = :u "
                    "AND rule_type = 'MAX_DAILY_TURNOVER'"
                ),
                {"u": user_id},
            )
        ).scalar_one()
        assert count == 1

        await db.rollback()
