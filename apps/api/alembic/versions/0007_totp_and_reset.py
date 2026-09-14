"""Mandatory TOTP: user secret columns, recovery codes, password resets.

Existing users get totp_enabled_at = NULL, which the VerifiedUser dependency
reads as "not enrolled" — they are routed to setup on their next request and
can reach nothing else until they finish. Signing in fresh yields an
enrolment-only session (see sessions.enrolment_only below) that authorises the
setup endpoints and nothing more, so an instance that makes 2FA mandatory does
not lock out the accounts that already exist on it.

IF NOT EXISTS guards follow the 0003-0006 convention: migration 0001 bootstraps
from live model metadata, so fresh databases already have these.
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column, ddl in (
        ("totp_secret_enc", "VARCHAR(512)"),
        ("totp_enabled_at", "TIMESTAMPTZ"),
        ("password_changed_at", "TIMESTAMPTZ"),
    ):
        op.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {column} {ddl}")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS recovery_codes (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            code_hash VARCHAR(255) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            used_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_recovery_codes_user_id "
        "ON recovery_codes (user_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS password_resets (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash VARCHAR(128) NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_password_resets_token_hash "
        "ON password_resets (token_hash)"
    )

    # Sessions issued to finish enrolment. Existing sessions get false, so
    # anyone already signed in keeps a full session and is sent to setup by the
    # VerifiedUser dependency instead.
    op.execute(
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS "
        "enrolment_only BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS enrolment_only")
    op.execute("DROP TABLE IF EXISTS password_resets")
    op.execute("DROP TABLE IF EXISTS recovery_codes")
    for column in ("totp_secret_enc", "totp_enabled_at", "password_changed_at"):
        op.execute(f"ALTER TABLE users DROP COLUMN IF EXISTS {column}")
