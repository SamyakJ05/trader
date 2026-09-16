"""Mark real broker accounts live, because nothing else ever did.

environment defaults to 'paper' when an account is created, and the UI had no
control to change it. So an ICICI Breeze account holding real stock was stored
as a paper account, and the label was the least of it:

  * the strategy runner skips a non-live strategy on a live-only instance
    (engines/strategy/runner.py). It stays RUNNING and never evaluates --
    started, silent, and indistinguishable from working.
  * a non-live strategy reads simulated candles rather than real quotes, so
    anything that did run would trade off invented prices.

Only accounts for a real broker are touched. 'paper' accounts are left exactly
as they are: they ARE paper, and 0011 already archived them.

Deliberately not conditioned on ENABLE_PAPER_TRADING. A migration cannot read
application settings, and an account at a real broker, holding real stock and
placing real orders, is a live account on any instance -- that is a fact about
the account, not about the host.
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE broker_accounts
        SET environment = 'live'
        WHERE broker <> 'paper'
          AND environment = 'paper'
        """
    )
    # A strategy's own environment gates the same runner branch, so an account
    # corrected above would still sit behind a strategy stamped paper.
    op.execute(
        """
        UPDATE strategies s
        SET environment = 'live'
        FROM broker_accounts a
        WHERE s.broker_account_id = a.id
          AND a.broker <> 'paper'
          AND s.environment = 'paper'
        """
    )


def downgrade() -> None:
    # Deliberately not reversed. Sending these back to 'paper' would point a
    # strategy at simulated candles while it places orders at a real broker,
    # which is the exact failure this migration exists to remove.
    pass
