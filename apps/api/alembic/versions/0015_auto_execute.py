"""Autonomous AI trading: per-account opt-in, order provenance, daily cap.

Three things, because unattended trading needs all three to be safe:

- broker_accounts.auto_execute — the opt-in, per account and off by default,
  so turning it on is a deliberate act for one account rather than a mode the
  instance falls into.
- orders.auto_executed — provenance. The only durable answer to "did a person
  agree to this trade?"; the proposal it came from can be edited or deleted.
- MAX_AUTO_TRADES_PER_DAY — a ceiling on how often the loop may fire, which
  no existing rule bounds. The capital limits cap rupees, not frequency, so a
  malfunctioning loop placing many small orders stays under every one of them.

The new rule is backfilled for existing users. A rule that only new accounts
received would leave every current account with no frequency limit at the
moment the feature that needs it ships.
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

# Matches risk_defaults.DEFAULT_RULES. Conservative: ten unattended trades a
# day is a floor a person raises knowingly, not a tuned figure.
DEFAULT_MAX_AUTO_TRADES = 10


def upgrade() -> None:
    # IF NOT EXISTS guards follow the 0003/0004/0005 convention: migration 0001
    # bootstraps the schema from live model metadata, so a fresh database
    # already has every column later migrations add.
    op.execute(
        "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS "
        "auto_execute BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
        "auto_executed BOOLEAN NOT NULL DEFAULT false"
    )
    # The daily-cap rule counts this column over one IST day, per user and
    # environment; without the index that is a sequential scan of orders on
    # every automatic order placed. Partial, because only automatic orders are
    # ever counted and they are a small minority of the table.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_orders_auto_executed "
        "ON orders (user_id, environment, placed_at) WHERE auto_executed"
    )

    # Backfill the new rule for every user that already has rules, in both
    # environments, without disturbing any limit they have edited.
    # The limit is interpolated rather than bound: `:params::jsonb` reads the
    # cast's colon as the start of another bind parameter, so the statement
    # never receives one. It is an integer constant defined in this file, not
    # user input.
    op.execute(
        sa.text(
            f"""
            INSERT INTO risk_rules
                (id, user_id, environment, rule_type, params, enabled, created_at)
            SELECT gen_random_uuid(), u.id, e.environment, 'MAX_AUTO_TRADES_PER_DAY',
                   '{{"max_auto_trades": {DEFAULT_MAX_AUTO_TRADES}}}'::jsonb, true, now()
            FROM users u
            CROSS JOIN (VALUES ('paper'), ('live')) AS e(environment)
            WHERE NOT EXISTS (
                SELECT 1 FROM risk_rules r
                WHERE r.user_id = u.id
                  AND r.environment = e.environment
                  AND r.rule_type = 'MAX_AUTO_TRADES_PER_DAY'
            )
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM risk_rules WHERE rule_type = 'MAX_AUTO_TRADES_PER_DAY'")
    )
    op.execute("DROP INDEX IF EXISTS ix_orders_auto_executed")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS auto_executed")
    op.execute("ALTER TABLE broker_accounts DROP COLUMN IF EXISTS auto_execute")
