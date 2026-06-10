"""Add broker_accounts.read_verified_at — timestamp of the last successful
read-path verification (profile + funds fetched from the broker).

IF NOT EXISTS guard is required: migration 0001 bootstraps the schema from
live model metadata, so fresh databases already have columns added by later
migrations. Every post-0001 column migration needs the same guard until 0001
is frozen to a point-in-time schema."""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE broker_accounts "
        "ADD COLUMN IF NOT EXISTS read_verified_at TIMESTAMP WITH TIME ZONE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE broker_accounts DROP COLUMN IF EXISTS read_verified_at")
