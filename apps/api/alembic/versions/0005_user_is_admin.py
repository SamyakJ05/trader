"""Add users.is_admin — operator flag gating cross-tenant actions
(today: the global kill switch, which halts every user's strategies).

IF NOT EXISTS guard follows the 0003/0004 convention: migration 0001
bootstraps from live model metadata, so fresh databases already have it.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "is_admin BOOLEAN NOT NULL DEFAULT false"
    )
    # Data repair: strategies bound to a broker account owned by a different
    # user. Creating one is blocked from this release onward, but rows written
    # before that validation existed are still live — and the runner loads
    # them every tick, calls place_order, and now takes an OrderServiceError.
    # Unbind and mark them ERROR so they stop cleanly instead.
    op.execute(
        """
        UPDATE strategies AS s
           SET broker_account_id = NULL,
               status = 'ERROR'
          FROM broker_accounts AS ba
         WHERE s.broker_account_id = ba.id
           AND ba.user_id <> s.user_id
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS is_admin")
