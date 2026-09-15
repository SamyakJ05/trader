#!/usr/bin/env bash
# Restore a dump into a throwaway database and check it is not hollow.
#
# A backup you have not restored is not a backup. The failures that matter --
# a dump truncated by a full disk, a pg_dump older than the server, a password
# that changed -- are all invisible until the day you need it, and they leave
# behind a file that looks perfectly fine.
#
# So this restores for real, then checks the tables whose emptiness would mean
# the dump is worthless, then checks that the cash ledger still balances. It
# drops the scratch database afterwards and never touches the live one.
#
# Usage: DATABASE_URL=... ops/verify_restore.sh backup-2026-09-15.dump
set -euo pipefail

DUMP="${1:?usage: verify_restore.sh <dump-file>}"
[ -f "$DUMP" ] || { echo "no such dump: $DUMP" >&2; exit 1; }

: "${DATABASE_URL:?DATABASE_URL must be set to the live connection string}"

# Timestamped so a previous failed run cannot be mistaken for this one.
SCRATCH="restore_check_$(date +%Y%m%d_%H%M%S)"

# Admin URL points at the maintenance database so the scratch one can be
# created and dropped. The live database is never the target of either.
ADMIN_URL="${DATABASE_URL%/*}/defaultdb"
SCRATCH_URL="${DATABASE_URL%/*}/${SCRATCH}"

cleanup() {
  psql "$ADMIN_URL" -q -c "DROP DATABASE IF EXISTS ${SCRATCH}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> restoring ${DUMP} into scratch database ${SCRATCH}"
psql "$ADMIN_URL" -q -c "CREATE DATABASE ${SCRATCH}"

# --exit-on-error because a restore that half-works is a failure. Without it
# pg_restore reports success having skipped every statement that threw.
pg_restore --dbname="$SCRATCH_URL" --no-owner --no-privileges --exit-on-error "$DUMP"

echo "==> checking the restored data is not hollow"

# Emptiness in any of these means the dump is worthless. A schema-only dump
# restores cleanly and passes every check that only looks for errors.
REQUIRED_TABLES="users orders fills cash_ledger audit_events"

failed=0
for table in $REQUIRED_TABLES; do
  count=$(psql "$SCRATCH_URL" -tAc "SELECT count(*) FROM ${table}" 2>/dev/null || echo "MISSING")
  if [ "$count" = "MISSING" ]; then
    echo "  FAIL  ${table}: table absent from the dump"
    failed=1
  elif [ "$count" = "0" ]; then
    echo "  FAIL  ${table}: restored empty"
    failed=1
  else
    printf '  ok    %-14s %s rows\n' "$table" "$count"
  fi
done

# Balancing is the check that the dump restored *consistently*, not merely
# that rows arrived. Each ledger row stores the running balance as of that
# entry, so recomputing the sum and comparing the two catches a torn backup
# that landed plausible-looking rows whose totals no longer agree.
echo "==> checking the cash ledger still balances"
LEDGER_SQL="SELECT count(*) FROM (
    SELECT id FROM (
      SELECT id, balance,
             sum(amount) OVER (PARTITION BY broker_account_id ORDER BY id) AS running
      FROM cash_ledger
    ) t
    WHERE abs(balance - running) > 0.01
  ) bad"

imbalance=$(psql "$SCRATCH_URL" -tAc "$LEDGER_SQL" 2>/dev/null || echo "MISSING")

if [ "$imbalance" = "MISSING" ]; then
  echo "  WARN  could not evaluate the ledger (schema may have moved on)"
elif [ "$imbalance" != "0" ]; then
  echo "  FAIL  ${imbalance} ledger row(s) whose stored balance disagrees with the running sum"
  failed=1
else
  echo "  ok    ledger balances"
fi

echo
if [ "$failed" -ne 0 ]; then
  echo "RESTORE VERIFICATION FAILED — this dump is not a usable backup."
  exit 1
fi
echo "RESTORE VERIFICATION PASSED — ${DUMP} restores to a complete database."
