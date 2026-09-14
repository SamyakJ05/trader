"""Auth hardening: invites table, session device columns.

Sessions become device records (user_agent / ip / last_seen_at) and are
revoked by setting revoked_at rather than being deleted, so a signed-out
device stays visible in the user's device list and in the audit trail.

IF NOT EXISTS guards follow the 0003/0004/0005 convention: migration 0001
bootstraps from live model metadata, so fresh databases already have these.
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS invites (
            id UUID PRIMARY KEY,
            email VARCHAR(255) NOT NULL,
            token_hash VARCHAR(128) NOT NULL UNIQUE,
            invited_by UUID REFERENCES users(id) ON DELETE SET NULL,
            full_name VARCHAR(255),
            as_admin BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            consumed_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_invites_email ON invites (email)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_invites_token_hash ON invites (token_hash)"
    )

    for column, ddl in (
        ("user_agent", "VARCHAR(512)"),
        ("ip", "VARCHAR(64)"),
        ("last_seen_at", "TIMESTAMPTZ"),
        ("revoked_at", "TIMESTAMPTZ"),
    ):
        op.execute(f"ALTER TABLE sessions ADD COLUMN IF NOT EXISTS {column} {ddl}")


def downgrade() -> None:
    for column in ("user_agent", "ip", "last_seen_at", "revoked_at"):
        op.execute(f"ALTER TABLE sessions DROP COLUMN IF EXISTS {column}")
    op.execute("DROP TABLE IF EXISTS invites")
