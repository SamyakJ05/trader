"""Archive paper accounts on a live-only instance.

The instance is moving to live trading only. Paper accounts stop being places
where trades happen, so the ones that already exist are stopped rather than
left looking active: a RUNNING strategy the runner silently skips, or an
account showing "connected" that accepts no orders, is worse than one plainly
marked inactive.

NOTHING IS DELETED. Positions, holdings, the cash ledger, orders, fills and
audit history are all untouched and stay readable. This only changes two
status columns, and both changes are reversible -- restarting a strategy and
reconnecting a paper account are ordinary UI actions.

This runs unconditionally rather than reading ENABLE_PAPER_TRADING, because a
migration cannot see application settings and guessing from the environment
would make the schema depend on which host ran it. An instance that keeps
paper enabled can simply restart the strategies it wants; they are drafts and
stopped strategies, not lost ones.
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # STOPPED, not KILLED or ERROR. The strategies are not broken and nobody
    # engaged a kill switch -- the instance changed underneath them, and
    # STOPPED is the state a user can restart from without explanation.
    op.execute(
        """
        UPDATE strategies
        SET status = 'STOPPED'
        WHERE environment = 'paper'
          AND status = 'RUNNING'
        """
    )
    # Paper accounts report "connected" from creation because there is no
    # broker to connect to. On a live-only instance that reads as ready to
    # trade, which it no longer is.
    op.execute(
        """
        UPDATE broker_accounts
        SET status = 'disconnected',
            status_message = 'Paper trading is disabled on this instance; '
                             'history is preserved and read-only.'
        WHERE broker = 'paper'
          AND status = 'connected'
        """
    )


def downgrade() -> None:
    # Deliberately not reversed. Restoring RUNNING would restart strategies
    # that a person stopped in the meantime, and reconnecting accounts would
    # claim a readiness this migration cannot verify. Both are one click in
    # the UI, which is the right place for a decision about what should trade.
    pass
