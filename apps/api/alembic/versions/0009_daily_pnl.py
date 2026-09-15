"""Durable daily realized P&L.

The figure MAX_DAILY_LOSS compares against lived only in Redis, where a flush
without persistence reset it to zero mid-day — the rule went on evaluating and
passing while protecting nothing. Redis stays the hot path; this is what it is
rebuilt from.

IF NOT EXISTS guard follows the 0003-0008 convention: migration 0001
bootstraps from live model metadata, so fresh databases already have it.
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_pnl (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            environment VARCHAR(8) NOT NULL,
            trading_day DATE NOT NULL,
            realized NUMERIC(18, 2) NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_daily_pnl_user_env_day
                UNIQUE (user_id, environment, trading_day)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS daily_pnl")
