"""initial schema

Bootstrap migration. Creates the schema as it stood at 0001, NOT the current
model metadata.

It used to call Base.metadata.create_all() unfiltered, which builds whatever
the models happen to say today. That made a fresh database arrive at 0001
already holding tables and columns that later migrations then tried to create
again:

    alembic upgrade head
    -> DuplicateTableError: relation "news_items" already exists

so `upgrade head` could not run at all on a new database, and nothing was
stamped. Column migrations had been absorbing this individually with
IF NOT EXISTS guards (see 0003, 0005), but 0008 and 0014 create tables and
had no such guard.

Excluding the later tables here fixes it at the source: 0001 stops claiming
to be the current schema, and every subsequent migration gets the database
state it was written against, whether it is upgrading an existing deployment
or building a new one. The IF NOT EXISTS guards in the column migrations are
left as they are -- they are harmless, and existing databases were created
under the old behaviour.

Adding a new table from here on needs no change to this file: write the
migration that creates it, and add its name to TABLES_ADDED_AFTER_0001 so a
fresh database does not get it early.
"""

from alembic import op

from app.db import models  # noqa: F401
from app.db.base import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Tables introduced by later migrations, which this bootstrap must not create.
# Keyed by the migration that owns each one, so the reason a name is here
# stays visible.
TABLES_ADDED_AFTER_0001 = {
    # 0008_honest_paper
    "candles",
    "backtest_runs",
    "holdings",
    "cash_ledger",
    "pending_settlements",
    # 0014_news_items
    "news_items",
}


def _tables_at_0001():
    return [
        table
        for name, table in Base.metadata.tables.items()
        if name not in TABLES_ADDED_AFTER_0001
    ]


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=_tables_at_0001())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), tables=_tables_at_0001())
