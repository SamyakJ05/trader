# Operations runbook

**Written for: the operator running this instance — you, or whoever holds the
droplet after you.**

Everything here assumes the production overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

---

## What is where

| Thing | Lives in | Survives a droplet rebuild? |
|---|---|---|
| Orders, fills, ledger, audit log, users | DigitalOcean Managed Postgres | Yes |
| Kill switches, rate-limit counters, worker heartbeat | Redis, in a container | No, and that is fine |
| Broker API keys and secrets | `.env` on the droplet | **No — you hold the only copy** |
| Session tokens (encrypted) | Postgres | Yes, but they expire at midnight IST anyway |

Redis is deliberately disposable. The daily realized-P&L figure that
`MAX_DAILY_LOSS` reads is written to Postgres as well, so a Redis flush costs
you cached state and nothing that gates money.

`.env` is the exception to everything being recoverable. It holds
`APP_ENCRYPTION_KEY`, and **without that key every stored TOTP secret and
broker session token is unreadable**. Back it up somewhere that is not the
droplet — a password manager is fine. Losing it means every user re-enrols
their second factor.

---

## Backups

Managed Postgres takes daily backups with point-in-time recovery, retained
seven days on the basic plan. That covers hardware failure and a bad
migration.

It does not cover the thing most likely to happen: someone deleting rows they
meant to keep, and noticing a week later. So take your own dumps too, and keep
them longer.

```bash
# On the droplet, with DATABASE_URL set to the managed instance.
make backup
```

That runs `pg_dump` from a throwaway `postgres` image rather than the api
container — the api image carries no client tools, and pinning the image to
the server's major version (`PG_MAJOR`, default 16) avoids the version skew
that silently produces a dump you cannot restore. Check it matches your
managed instance.

**A backup you have not restored is not a backup.** The failure modes that
matter — a dump truncated by a full disk, a password that changed, a
`pg_dump` version older than the server — are all invisible until the day you
need it. So restore it, on a schedule, into a throwaway database:

```bash
DATABASE_URL=... make verify-restore file=backup-2026-09-15.dump
```

That creates a scratch database, restores into it, and checks the tables that
would tell you the dump is hollow: users, orders, fills, the cash ledger and
the audit log all have to come back non-empty, and the ledger has to still
balance. It drops the scratch database afterwards. It never touches the live
one.

Do this monthly, and after any change to the backup path.

---

## Monitoring

Two endpoints, and they answer different questions.

`GET /api/v1/healthz` — is this api process and its dependencies up? Returns
503 when not. This is what a container orchestrator should restart on.

`GET /api/v1/readyz` — the above, plus: is the worker still ticking? Returns
503 when not, and reports `worker_last_beat_age_seconds`. **This is the one to
put in an uptime monitor.** A dead worker leaves every page rendering
correctly while nothing fills, which is invisible from the outside.

Point a free uptime monitor (UptimeRobot, Better Stack, Healthchecks.io) at
`/readyz` every minute and have it alert you. That single check covers
Postgres, Redis and the worker.

### Logs

```bash
docker compose logs -f api worker           # follow
docker compose logs --since 1h worker       # recent
docker compose logs api | grep egress_ip    # outbound address at startup
```

Logs are JSON in production. Events worth knowing by name:

| Event | Means |
|---|---|
| `egress_ip_mismatch` | This host is not leaving from the IP registered with the broker. **Every order will be rejected.** Reads still work. |
| `price_stage_failed`, `fill_stage_failed`, `settlement_stage_failed` | One stage of the tick threw. Others continued. Repeated occurrences are a bug, not a blip. |
| `live_fill_booked` | A real fill was accounted for. `estimated_charges=true` means our figure, not the broker's. |
| `breeze_stream_dropped_ticks` | The tick queue overflowed behind a slow consumer. |

---

## The outbound IP

ICICI whitelist one primary and one secondary address, and the requirement
covers **transactional requests only** — placing, modifying, cancelling. Market
data, positions, the order book and the daily browser login all work from
anywhere.

Set `BROKER_STATIC_IP` in `.env` to the address you registered. The api then
logs `egress_ip_mismatch` at error level on startup when it differs, so a
rebuilt droplet or a detached reserved IP shows up in the first lines of the
log rather than in a rejected order during market hours.

To see what this host actually leaves from:

```bash
docker compose exec api python -c \
  "import asyncio; from app.core.egress import detect_egress_ip; print(asyncio.run(detect_egress_ip()))"
```

**Attach a reserved IP to the droplet before registering it.** A droplet's
default public IP is not stable across a rebuild, and ICICI allow changing the
registered address roughly once a week.

---

## Routine tasks

```bash
make migrate                     # apply migrations after a deploy
make bootstrap-admin email=you@example.com   # first operator; registration is invite-only
docker compose restart worker    # safe any time: the tick is idempotent
```

### Importing history for a backtest

**Do this from the UI**: Backtests → Import history. It queues a worker job
and reports, per symbol, how many candles landed and what failed. The CLI
below is the fallback for when the UI or the worker is not available.

A backtest replays stored candles; it cannot invent bars it does not have.
Nothing imports history automatically, and the strategies most worth testing
are usually the ones whose symbols were never imported.

```bash
docker compose exec api python -m app.cli.import_history \
  --symbol RELIANCE --interval 1d --start 2024-01-01 --end 2026-01-01
```

One symbol per run, and always the **NSE ticker** — the importer appends
`.NS` for Yahoo. The UI takes up to ten symbols at once. A Breeze strategy names ICICI's own codes (RELIND), and the
backtest resolves those to the NSE ticker through the instrument master's
ISIN, so sync instruments before importing or the mapping has nothing to
work from.

Watch for the split warnings it prints. Yahoo's unadjusted data records a
split as a genuine overnight collapse, and a backtest spanning that date
reads it as a price move — the equity curve looks catastrophic for a reason
that has nothing to do with the strategy.

Intraday history is retention-limited at the source — Yahoo serves only a
recent window of 1m bars, well short of a year — so a long intraday backtest
is not available regardless of what dates you ask for. An empty import with
the message "check symbol, dates and intraday retention" usually means the
range is older than that window, not that the symbol is wrong.

### Deploying a change

```bash
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
make migrate
curl -fsS localhost:8000/api/v1/readyz | jq
```

Migrations run after the new image is up. Check `/readyz` before walking away
— a worker that fails to start is otherwise silent.

---

## When something is wrong

**Orders rejected, reads fine.** Check `egress_ip_mismatch` first. It is the
cause that looks like the other two.

**Nothing is filling.** `curl localhost:8000/api/v1/readyz`. If `worker` is
false, `docker compose logs worker`.

**Everything is slow, or connections are refused.** You may be over the
managed-Postgres connection cap. `DB_POOL_SIZE` × (api workers + 1) has to
stay under it — see `apps/api/app/db/session.py`.

**You need to stop trading right now.** The kill switch is in the UI and takes
effect on the next tick. To stop harder: `docker compose stop worker`. No new
orders are placed while it is down; resting broker orders are unaffected and
must be cancelled at the broker.
