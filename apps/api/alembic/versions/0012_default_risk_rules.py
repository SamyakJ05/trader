"""Give every existing account the default risk rules.

The risk engine loops over a user's ENABLED rules, so a user with none passes
every check: no order cap, no position cap, no daily-loss stop. Only the demo
seeder ever created rules, and a production deploy never runs it -- the
DigitalOcean runbook goes bootstrap-admin then straight to connecting a
broker. So every real account had an empty rule set and nothing enforced.

That was survivable while the platform was paper-only. It is not survivable
on a live-only instance about to run unattended AI strategies, which is what
makes this a migration rather than a note in the runbook.

created_at is supplied explicitly. The column is NOT NULL and its default is
declared on the ORM model (default=utcnow), which is Python-side only -- raw
SQL bypasses it entirely. CI did not catch this because its database has no
users, so the INSERT matched zero rows and succeeded trivially.

Inserted only where a user has no rule of that type in that environment, so
re-running is safe and anyone who already tuned a limit keeps it. Values match
app/services/risk_defaults.py; new accounts get the same set at registration.
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

# (rule_type, params JSON). Deliberately conservative: a floor to be raised
# knowingly, not a ceiling tuned for anyone's strategy.
_RULES = [
    ("MAX_TOTAL_EXPOSURE", '{"max_exposure": 100000}'),
    ("MAX_DAILY_TURNOVER", '{"max_turnover": 200000}'),
    ("MAX_ORDER_NOTIONAL", '{"max_notional": 25000}'),
    ("MAX_POSITION_SIZE", '{"max_quantity": 100}'),
    ("MAX_OPEN_POSITIONS", '{"max_positions": 5}'),
    ("MAX_DAILY_LOSS", '{"max_loss": 5000}'),
    ("DUPLICATE_ORDER_COOLDOWN", '{"seconds": 5}'),
    ("MARKET_HOURS", "{}"),
]


def upgrade() -> None:
    for environment in ("paper", "live"):
        for rule_type, params in _RULES:
            op.execute(
                f"""
                INSERT INTO risk_rules
                    (id, user_id, environment, rule_type, params, enabled, created_at)
                SELECT gen_random_uuid(), u.id, '{environment}', '{rule_type}',
                       '{params}'::jsonb, true, now()
                FROM users u
                WHERE NOT EXISTS (
                    SELECT 1 FROM risk_rules r
                    WHERE r.user_id = u.id
                      AND r.environment = '{environment}'
                      AND r.rule_type = '{rule_type}'
                )
                """
            )


def downgrade() -> None:
    # Deliberately not reversed. Deleting rules by type would remove limits a
    # person has since edited and now relies on, and the failure mode of
    # removing a risk limit is unbounded. Disable them in the UI instead.
    pass
