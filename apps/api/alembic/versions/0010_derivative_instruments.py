"""Let the instrument master and an order hold a derivatives contract.

market_instruments was unique on (broker, exchange, symbol), which cannot
represent an options chain: Breeze's F&O security master carries 79,612
contracts under only 216 distinct stock codes -- NIFTY alone has 3,350. Under
the old constraint a sync would have kept one row per underlying and silently
dropped the other 79,396, so no strategy could ever name a specific contract
and no F&O order could be built.

A contract is identified by (broker, exchange, symbol, expiry, strike, right).
The uniqueness moves there, and strike/option_right are added alongside the
expiry column that already existed. The column is option_right rather than
right because RIGHT is a reserved SQL keyword -- usable only quoted, which
every hand-written query would then have to remember.

Postgres treats NULLs as distinct in a unique constraint, which would let
duplicate cash-segment rows accumulate (every equity row has all three NULL).
The index therefore uses COALESCE sentinels so equities still collide on
(broker, exchange, symbol) exactly as before.

IF NOT EXISTS guards follow the 0003-0009 convention: migration 0001
bootstraps from live model metadata, so fresh databases already have the
columns.
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # An order must remember which contract it was for. modify_order rebuilds
    # an OrderRequest from this row to send to the broker, and Breeze marks
    # expiry/right/strike mandatory on PUT /order as well as POST -- so
    # without these an F&O order could be placed and then never amended. The
    # reconciler likewise cannot say which contract a row refers to.
    op.execute(
        """
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS expiry DATE,
            ADD COLUMN IF NOT EXISTS strike NUMERIC(18, 4),
            ADD COLUMN IF NOT EXISTS option_right VARCHAR(8)
        """
    )
    op.execute(
        """
        ALTER TABLE market_instruments
            ADD COLUMN IF NOT EXISTS strike NUMERIC(18, 4),
            ADD COLUMN IF NOT EXISTS option_right VARCHAR(8)
        """
    )
    # The old constraint's name is whatever Postgres generated for the
    # UniqueConstraint in 0001; drop by lookup rather than by guessed name.
    op.execute(
        """
        DO $$
        DECLARE con text;
        BEGIN
            SELECT conname INTO con
            FROM pg_constraint
            WHERE conrelid = 'market_instruments'::regclass
              AND contype = 'u'
              AND pg_get_constraintdef(oid) LIKE '%broker%exchange%symbol%'
              AND pg_get_constraintdef(oid) NOT LIKE '%expiry%';
            IF con IS NOT NULL THEN
                EXECUTE format(
                    'ALTER TABLE market_instruments DROP CONSTRAINT %I', con
                );
            END IF;
        END $$
        """
    )
    # A unique INDEX rather than a constraint, because the expression form
    # (COALESCE) cannot be a table constraint. ON CONFLICT targets it by the
    # same expression list, which is what the upsert in
    # services/instruments.py names.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_instruments_contract
        ON market_instruments (
            broker,
            exchange,
            symbol,
            COALESCE(expiry, DATE '1900-01-01'),
            COALESCE(strike, -1),
            COALESCE(option_right, '')
        )
        """
    )
    # Strategies resolve a chain by underlying and expiry; without this that
    # is a sequential scan over ~80k rows per lookup.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_instruments_chain
        ON market_instruments (broker, exchange, symbol, expiry)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE orders
            DROP COLUMN IF EXISTS expiry,
            DROP COLUMN IF EXISTS strike,
            DROP COLUMN IF EXISTS option_right
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_instruments_chain")
    op.execute("DROP INDEX IF EXISTS uq_instruments_contract")
    # Restoring the old constraint would fail wherever F&O rows have already
    # been synced, since they collide on (broker, exchange, symbol) by design.
    # Deleting them to make it fit would destroy data the application is using,
    # so this leaves the table without that constraint and says so rather than
    # failing halfway or silently discarding rows.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM market_instruments
                WHERE expiry IS NOT NULL
                GROUP BY broker, exchange, symbol
                HAVING count(*) > 1
            ) THEN
                ALTER TABLE market_instruments
                    ADD CONSTRAINT market_instruments_broker_exchange_symbol_key
                    UNIQUE (broker, exchange, symbol);
            ELSE
                RAISE NOTICE
                    'Derivative rows share a symbol; the old unique constraint '
                    'was not restored. Delete F&O rows first if it is needed.';
            END IF;
        END $$
        """
    )
    op.execute(
        """
        ALTER TABLE market_instruments
            DROP COLUMN IF EXISTS strike,
            DROP COLUMN IF EXISTS option_right
        """
    )
