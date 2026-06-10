# trader — broker-agnostic algo trading platform (India)

Production-minded MVP. Connects to Zerodha Kite Connect, Groww Trading API and
ICICI Direct Breeze through a single broker-adapter interface. **Paper trading
is the default and only enabled execution mode.** Live trading is triple-gated
and off everywhere.

## Architecture

```
apps/web   Next.js 15 + TS + Tailwind     dashboard, brokers, orders, positions,
                                          strategies, risk, audit
apps/api   FastAPI (Python 3.12)
  app/adapters/      broker isolation boundary — ONLY place broker APIs are touched
    base.py          abstract BrokerAdapter (connect/refreshSession/getProfile/
                     getFunds/getHoldings/getPositions/getOrders/placeOrder/
                     modifyOrder/cancelOrder/getInstruments/subscribeTicks)
    paper/           WORKING — internal simulator behind the same interface
    zerodha/         SCAFFOLD — Kite Connect v3 REST + checksum auth wired
    groww/           SCAFFOLD — token auth wired, paths need verification
    icici_breeze/    SCAFFOLD — request-signing (checksum) auth wired
  app/engines/
    paper/           fill simulation, partial fills, positions, P&L, charges hook
    risk/            pre-trade checks, kill switches, market-hours guard
    strategy/        strategy base + SMA crossover example + runner
  app/services/      order pipeline (idempotency -> audit -> risk -> dispatch),
                     broker lifecycle, audit emitter, kill switch
  app/api/routes/    REST surface (OpenAPI at /docs)
  app/workers/       arq jobs: price tick -> fills -> mark-to-market -> strategies
Postgres             system of record (18 tables, Alembic migrations)
Redis                sim prices, kill switches, cooldowns, job queue
```

Key invariants:
- Nothing outside `app/adapters/` talks to a broker. Adapters normalize all
  responses into internal domain models and map broker enums internally.
- Every order goes through one pipeline: idempotency check → `ORDER_REQUESTED`
  audit → risk engine → dispatch → audited state transitions.
- Live dispatch requires ALL of: `ENABLE_LIVE_TRADING=true` env,
  `live_enabled=true` on the account row, adapter status `working` in the
  capability matrix. Today no adapter is `working` except paper, so live
  orders are structurally impossible.
- Audit events (`audit_events`) are append-only: signals, risk checks, order
  requests, broker responses, state transitions, fills, user actions, kill
  switch flips, session events.

## Quick start

Prereqs: Docker + Docker Compose.

```bash
make setup        # copies .env.example -> .env (edit if you want)
make up           # postgres, redis, api, worker, web
make migrate      # alembic upgrade head
make seed         # demo user + paper account + risk rules + demo strategy
```

Then:
- Web UI: http://localhost:3000 — login `demo@trader.local` / `demo1234`
- API docs (OpenAPI): http://localhost:8000/docs
- Health: http://localhost:8000/api/v1/healthz

Try it: Orders page → place a MARKET BUY for `RELIANCE` → watch it fill within
a tick, position appears on Positions, every step lands in the Audit Log.
Start the seeded demo strategy on Strategies to see signal → risk → order flow.

`MARKET_HOURS_ENFORCED=false` in `.env.example` so paper trading works at any
hour; set `true` to enforce NSE hours (09:15–15:30 IST, Mon–Fri).

Useful targets: `make logs`, `make test`, `make lint`, `make psql`,
`make reset-db` (nuke + remigrate + reseed).

## What is scaffolded vs working

**Working (exercised end-to-end locally):**
- Local auth (register/login/logout, DB-backed sessions)
- Multi-account broker connection records with status + last-sync
- Paper trading: market/limit/SL/SL-M orders, fills, partial fills, cancels,
  modify, positions, realized/unrealized P&L, virtual cash, account reset
- Risk engine: max daily loss, max order notional, max position size, max open
  positions, duplicate-order cooldown, market-hours guard
- Global + per-strategy kill switches (Redis-backed, audited, UI-controlled)
- Idempotent order placement (`client_order_id` unique per account; replays —
  including concurrent races — return the original order)
- Daily-loss tracking via a per-day realized-P&L counter in Redis, fed at fill
  time (positions store cumulative P&L, which cannot answer the daily question)
- Append-only audit enforced at the database level (trigger rejects
  UPDATE/DELETE on `audit_events`, migration 0002)
- Event-sourced audit log with filtering UI
- Strategy runner with SMA-crossover example on the simulated feed
- Background worker (arq): price ticks, fill processing, mark-to-market,
  strategy orchestration
- Dashboard UI for all of the above, with paper/live and capability badges

**Scaffolded (wired per docs, NOT verified against real brokers):**
- Zerodha adapter: login URL, request-token → access-token checksum exchange,
  callback route, profile/funds/holdings/positions/orders/instruments calls,
  order payloads with `tag` idempotency. Untested against a live Kite app.
  WebSocket ticks not implemented.
- Groww adapter: bearer-token auth, read endpoints drafted with
  `TODO(verify path)` markers. TOTP token-generation flow not implemented.
  Trading methods deliberately raise.
- ICICI Breeze adapter: login URL, session exchange, per-request SHA-256
  checksum signing, read endpoints drafted. Trading methods deliberately raise.
  Documented rate limits (100/min, 5000/day) noted in the capability matrix;
  client-side throttling not yet implemented.
- Zerodha postback webhook: stores + audits events; checksum validation and
  order reconciliation are TODO.
- Charges engine: placeholder hook returning 0 (real brokerage/STT/GST stack
  is TODO).
- Holdings model for paper (positions only today).

The capability matrix in `apps/api/app/domain/capabilities.py` is the single
source of truth and distinguishes *what the broker supports* from *what our
adapter implements* (`adapter_status`). The UI renders both. A regression test
(`tests/test_live_gate.py`) fails on purpose if any real broker's
`adapter_status` is flipped to `working`, so the flip is always a deliberate,
reviewed act.

## Known caveats

- The daily-loss counter lives in Redis: a Redis flush/restart without AOF
  persistence resets it to zero for the day. Enable AOF before relying on it.
- Kill switches halt order *placement*; already-open paper orders keep filling
  on worker ticks. Engage the switch, then cancel working orders from the
  Orders page if you want a hard stop.
- Migration `0001` bootstraps via `metadata.create_all`; every later schema
  change must use `alembic revision --autogenerate` (`make makemigration m=...`).
- Paper risk checks price orders off the simulated feed; live accounts would
  need real quotes (`TODO(live-quotes)` in `app/services/orders.py`) — moot
  while live dispatch is gated off.
- Strategy state `KILLED` does not auto-release: release the kill switch, then
  explicitly start the strategy again.

## How to add the first real Zerodha order flow

1. Create a Kite Connect app at https://developers.kite.trade. Set the redirect
   URL to `http://localhost:8000/api/v1/brokers/zerodha/callback`.
2. Put credentials in `.env`: `ZERODHA_MAIN_API_KEY`, `ZERODHA_MAIN_API_SECRET`.
   Set `APP_ENCRYPTION_KEY` (generate with the command in `.env.example`) so the
   daily access token is encrypted at rest.
3. In the UI, add a broker account: broker `zerodha`, credential ref
   `ZERODHA_MAIN`, environment `paper` for now. Click Connect → complete Kite
   login → callback stores the encrypted access token.
4. Verify read paths first: Sync the account; confirm profile/funds/holdings
   come back correctly (`apps/api/app/adapters/zerodha/adapter.py`). Fix any
   payload drift against current Kite docs.
5. Verify order path on paper... there is no Kite sandbox, so this means a
   real 1-quantity order: temporarily set `ENABLE_LIVE_TRADING=true`, set
   `live_enabled=true` on the account row, and flip
   `CAPABILITY_MATRIX[Broker.ZERODHA].adapter_status` to `WORKING` — all three
   gates exist precisely so this is a deliberate act. Place a 1-share CNC limit
   order far from market price, verify it appears in Kite, then cancel it
   through the API. Confirm `BROKER_RESPONSE` audit events captured everything.
6. Implement the postback checksum (`app/api/routes/webhooks.py` TODO) and order
   reconciliation so fills update the orders table.
7. Add a worker job to refresh/invalidate the Kite session daily (token dies
   ~6am IST) and surface `session_expired` on the brokers page.
8. Only then consider strategy-driven live orders — and start with hard-coded
   tiny quantity caps in a new risk rule.

## API surface (summary)

All under `/api/v1`. Full schemas at `/docs`.

| Area | Endpoints |
|---|---|
| auth | `POST /auth/register`, `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` |
| brokers | `GET/POST /brokers/accounts`, `DELETE /brokers/accounts/{id}`, `POST .../connect`, `POST .../reconnect` (refresh-session), `POST .../disconnect`, `POST .../verify` (read access), `POST .../sync`, `PUT .../session-token`, `PATCH .../live`, `GET /brokers/capabilities`, `GET /brokers/zerodha/callback` |
| dashboard | `GET /dashboard/summary` (accounts, funds, positions, orders, strategies, kill switch, recent audit — with partial-data flags) |
| portfolio | `GET /portfolio/summary`, `/funds`, `/holdings`, `/positions` |
| orders | `GET/POST /orders`, `GET /orders/{id}`, `POST /orders/{id}/cancel`, `PATCH /orders/{id}` |
| strategies | `GET/POST /strategies`, `GET /strategies/kinds`, `POST .../start`, `POST .../stop`, `DELETE`, `GET .../signals` |
| risk | `GET/POST /risk/rules`, `PATCH/DELETE /risk/rules/{id}`, `GET /risk/events` |
| system | `GET /healthz`, `GET /system/config`, `GET/POST /system/killswitch`, `POST /paper/accounts/{id}/reset` |
| audit | `GET /audit/events` (cursor-paginated, filterable) |
| webhooks | `POST /webhooks/zerodha` |

Example — place a paper order:

```bash
TOKEN=$(curl -s localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"demo@trader.local","password":"demo1234"}' | jq -r .token)

ACCOUNT=$(curl -s localhost:8000/api/v1/brokers/accounts \
  -H "Authorization: Bearer $TOKEN" | jq -r '.[0].id')

curl -s localhost:8000/api/v1/orders \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "broker_account_id": "'$ACCOUNT'",
    "client_order_id": "demo-001",
    "order": {
      "symbol": "RELIANCE", "exchange": "NSE", "side": "BUY",
      "order_type": "MARKET", "product": "MIS", "quantity": 10
    }
  }' | jq
```

Replaying with the same `client_order_id` returns the original order (idempotent).

## Broker connections from the frontend

The Brokers page (`/brokers`) drives the full connection lifecycle. Each card
shows two separate truth axes — **broker API capabilities** (what the broker
documents) and **adapter status** (how far our code actually got) — plus
read-path and trade-path chips:

- `read verified <date>` — profile + funds were actually fetched from the
  broker via “Verify read access” (sets `read_verified_at`).
- `trade path unverified` — shown for every real broker until its adapter is
  proven against a live account. Paper shows `trade path: working (paper)`.

What each broker requires:

| Broker | Needs | Flow from UI |
|---|---|---|
| paper | nothing | Create connection → done; Verify/Sync work immediately |
| zerodha | Kite app; `<REF>_API_KEY` / `<REF>_API_SECRET` in backend env; redirect URL `http://localhost:8000/api/v1/brokers/zerodha/callback` | Connect → Kite login → callback exchanges request_token via SHA-256 checksum → token stored encrypted → land back on `/brokers?connected=…` → Verify read access. Token expires daily; Reconnect re-validates, Connect re-runs login. |
| groww | daily access token (paste via “Set token”, stored encrypted, never re-displayed) or `<REF>_ACCESS_TOKEN` env | Create → Set token → Verify read access. Scaffolded: read paths unverified, trading disabled. |
| icici_breeze | `<REF>_API_KEY` / `<REF>_API_SECRET` env + session token from manual login at `api.icicidirect.com/apiuser/login?api_key=…` (not OAuth) | Create → Set token → Verify read access. Scaffolded: read paths unverified, trading disabled. |

API keys/secrets never pass through the frontend or database — only
short-lived session tokens do, Fernet-encrypted, with masked metadata
(`configured` + expiry) in responses. “Enable live” enforces the same
constraints as the order pipeline's triple gate and is refused with the exact
reason while no adapter is verified.

Testing the paper flow end-to-end: log in → Brokers → Create connection
(paper) → Verify read access (stamps the read chip) → Sync now (writes a funds
snapshot) → Dashboard shows the account with cash under Broker health.

Zerodha callback in local dev: the backend (not the Next app) receives the
redirect on port 8000, completes the token exchange, then 302s the browser to
`http://localhost:3000/brokers?connected={account_id}` (or `?error=…`), which
the Brokers page surfaces as a banner.

## Security notes

- Broker API keys/secrets live only in environment variables, referenced by
  `credential_ref` on the account row. Never in DB, never in code.
- Short-lived broker session tokens are Fernet-encrypted at rest
  (`APP_ENCRYPTION_KEY`); without a key set, dev fallback stores them marked
  plaintext — set the key before touching real credentials.
- Sessions are server-side DB tokens; passwords bcrypt-hashed.

## Troubleshooting

**Web container hangs or errors on first boot** — corepack needs the
`packageManager` field (present in `apps/web/package.json`). If you still hit
an interactive prompt, set `COREPACK_ENABLE_DOWNLOAD_PROMPT=0` on the web
service or run `pnpm install` locally inside `apps/web`.

**`make migrate` fails with "relation already exists"** — you likely ran the
API against a database created by an older checkout. `make reset-db` wipes the
volume and replays migrations + seed.

**`make seed` says user exists** — seeding is idempotent; that's normal.

**API can't reach Postgres on boot** — compose waits for healthchecks, but on
slow machines the first `make migrate` can race the DB. Re-run it.

**Orders stuck in ACCEPTED/OPEN forever** — the worker isn't running
(`docker compose logs worker`). Fills only happen on worker ticks; market
orders also fill inline at placement.

**Risk blocks everything with "Outside NSE market hours"** — set
`MARKET_HOURS_ENFORCED=false` in `.env` for after-hours paper trading, then
restart the api + worker containers.

**Tests** — `make test` runs the backend suite in the api container. Locally:
`uv pip install -e "apps/api[dev]"` then `cd apps/api && pytest -q` (must run
from `apps/api` so pytest picks up the asyncio config). The suite is pure-unit
(fakeredis, no Postgres needed). Frontend: `cd apps/web && pnpm test`
(vitest + testing-library component tests).

**Upgrading an existing dev DB** — new columns land via migrations
(`make migrate`); the brokers page will 500 until migration 0003
(`read_verified_at`) is applied.

## Roadmap

See [docs/ROADMAP.md](docs/ROADMAP.md).
