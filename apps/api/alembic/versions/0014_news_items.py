"""News and corporate announcements, for the analyst to read.

Advisory only by construction: nothing reads this table except the AI
analyst's tool, and the analyst's output is a proposal a human approves. An
external feed is the one input someone else can write to, and a feed that
breaks goes quiet rather than erroring -- "no bad news" and "the fetcher died"
look identical. Neither may move money unattended.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "news_items",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("source", sa.String(32), nullable=False),
        # Nullable: a market-wide headline belongs to no single company, and
        # attaching it to an arbitrary symbol would invent a relationship.
        sa.Column("symbol", sa.String(64), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        # server_default, not a Python default: rows are inserted by the
        # fetcher through the ORM today, but a backfill written in raw SQL
        # would otherwise hit the NOT NULL -- the exact way 0012 broke.
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Every source republishes its own items, and several carry the same
    # filing. Two rows for one announcement would read as two events, which
    # is a signal an analyst weighs.
    op.create_unique_constraint("uq_news_url", "news_items", ["url"])
    op.create_index("ix_news_published", "news_items", ["published_at"])
    op.create_index("ix_news_symbol_published", "news_items", ["symbol", "published_at"])


def downgrade() -> None:
    op.drop_index("ix_news_symbol_published", table_name="news_items")
    op.drop_index("ix_news_published", table_name="news_items")
    op.drop_constraint("uq_news_url", "news_items", type_="unique")
    op.drop_table("news_items")
