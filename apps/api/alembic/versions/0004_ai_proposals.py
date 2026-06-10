"""AI trading tables: ai_proposals (human-approved AI trade suggestions) and
ai_settings (per-user provider config with encrypted credentials).

IF NOT EXISTS guards follow the 0003 convention: migration 0001 bootstraps
from live model metadata, so fresh databases already contain these tables."""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_proposals (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            broker_account_id UUID NOT NULL REFERENCES broker_accounts(id) ON DELETE CASCADE,
            symbol VARCHAR(64) NOT NULL,
            exchange VARCHAR(8) NOT NULL,
            side VARCHAR(4) NOT NULL,
            order_type VARCHAR(8) NOT NULL,
            product VARCHAR(8) NOT NULL,
            quantity INTEGER NOT NULL,
            limit_price NUMERIC(18, 4),
            rationale TEXT NOT NULL,
            status VARCHAR(16) NOT NULL,
            order_id UUID REFERENCES orders(id) ON DELETE SET NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            decided_at TIMESTAMP WITH TIME ZONE
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ai_proposals_status ON ai_proposals (status)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_settings (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            provider VARCHAR(16) NOT NULL,
            model VARCHAR(128) NOT NULL,
            base_url VARCHAR(255),
            credentials_enc TEXT,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai_settings")
    op.execute("DROP TABLE IF EXISTS ai_proposals")
