# Tick or Trade

Algorithmic trading for Indian markets, at tickortrade.online.

Watch the feed, or act on it — the name is the platform's central
distinction. Paper and live are separate modes, and live sits behind three
gates you have to open on purpose.

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
    paper/           fill simulation, partial fills, positions, P&L, charges and settlement
    risk/            pre-trade checks, kill switches, market-hours guard
    strategy/        strategy base + SMA crossover example + runner
  app/services/      order pipeline (idempotency -> audit -> risk -> dispatch),
                     broker lifecycle, audit emitter, kill switch
  app/api/routes/    REST surface (OpenAPI at /docs)
  app/workers/       arq jobs: price tick -> fills -> mark-to-market -> strategies
Postgres             system of record (Alembic migrations)
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
  (seeded as an operator; invite other people from the Users page)
- API docs (OpenAPI): http://localhost:8000/docs
- Health: http://localhost:8000/api/v1/healthz

Try it: Orders page → place a MARKET BUY for `RELIANCE` → watch it fill within
a tick, position appears on Positions, every step lands in the Audit Log.
Start the seeded demo strategy on Strategies to see signal → risk → order flow.

`MARKET_HOURS_ENFORCED=false` in `.env.example` so paper trading works at any
hour; set `true` to enforce NSE hours (09:15–15:30 IST, Mon–Fri).

Useful targets: `make logs`, `make test`, `make lint`, `make psql`,
`make reset-db` (nuke + remigrate + reseed).

## AI trading (paper-only)

The AI Trading page adds three layers, all gated behind the same pipeline as
manual orders — the AI can never place an order directly:

- **Analyst chat** — an LLM with read-only tools over your positions, orders,
  funds, simulated quotes, strategies and risk rules. Trade ideas become
  `ai_proposals` rows that you approve or reject; approval routes through the
  standard order pipeline (idempotency → audit → risk → dispatch).
- **Strategy generator** — plain English → a validated strategy draft
  (constrained to registered strategy kinds), created as a normal DRAFT
  strategy via the strategies API.
- **`ai_agent` strategy kind** — the LLM decides BUY/SELL/HOLD per symbol on
  the worker tick, guarded by a per-symbol interval and a daily decision cap;
  decisions become ordinary signals through the risk engine and kill switches.
  Every decision (including HOLD) is audited as `AI_DECISION`.

Providers: Anthropic Claude, OpenAI, OpenRouter, or Amazon Bedrock —
configured per user on the AI Trading page (keys Fernet-encrypted in
`ai_settings`, never re-displayed), or via the `ANTHROPIC_API_KEY` env
fallback. Without a provider the rest of the app works unchanged.

## What is scaffolded vs working

**Working (exercised end-to-end locally):**
- Local auth (register/login/logout, DB-backed sessions)
- Multi-account broker connection records with status + last-sync
- Paper trading: market/limit/SL/SL-M orders, fills, partial fills, cancels,
  modify, positions, net realized/unrealized P&L, append-only cash ledger, account reset
- Equity statutory charges and broker-specific brokerage plans; paper accounts
  use zero brokerage (statutory charges and delivery DP charges still apply)
- CNC holdings with T+1 settlement across the checked-in NSE calendar
- Persistent 1m/5m candles, Yahoo historical import, SMA crossover backtests
  with next-open fills, saved metrics and an equity curve at `/backtests`
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
- Historical AI replay: not supported without historical context and recorded decisions.
  The backtest API currently exposes the registered SMA crossover strategy.

The capability matrix in `apps/api/app/domain/capabilities.py` is the single
source of truth and distinguishes *what the broker supports* from *what our
adapter implements* (`adapter_status`). The UI renders both. A regression test
(`tests/test_live_gate.py`) fails on purpose if any real broker's
`adapter_status` is flipped to `working`, so the flip is always a deliberate,
reviewed act.

## Known caveats

- The daily-loss counter is written to Postgres as well as Redis, so a cache
  flush no longer resets it. Redis stays the hot path; a cold read falls back
  to the ledger and repopulates it.
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

## Going live

Two brokers are built out to the same point: everything that can be written
without credentials is written, and nothing has been run against a real API.

- **[ICICI Breeze](docs/breeze-verification-playbook.md)** — stock codes rather
  than NSE symbols, no market orders, sessions dying at midnight IST, and every
  request signed with a checksum over the exact serialized body.
- **[Zerodha Kite](docs/zerodha-verification-playbook.md)** — bearer-token
  auth, a binary tick protocol via their SDK, sessions dying at the ~6am
  exchange flush.

Each playbook ends with one supervised single-share order on a real account,
because neither broker has a sandbox. The three gates (`ENABLE_LIVE_TRADING`,
the account's `live_enabled`, the adapter's status) are separate so that flip
is deliberate, and `tests/test_live_gate.py` fails the moment the third moves.

## Going live with Zerodha

Every Kite call here was written from Zerodha's documentation and has never
touched their API. `adapter_status` is `scaffold` and the live gate refuses
live orders structurally because of it.

**[docs/zerodha-verification-playbook.md](docs/zerodha-verification-playbook.md)**
is the sequence that changes that: connect, verify each read path against your
own Kite dashboard, sync instruments, watch the tick stream during market
hours, then one supervised 1-share limit order.

That last step spends real money on your own account — Kite has no sandbox, so
there is no way to prove the order path without placing an order. The three
gates (`ENABLE_LIVE_TRADING`, the account's `live_enabled`, and the adapter's
status) are separate so that flip is a deliberate act, and
`tests/test_live_gate.py` fails the moment the third one moves.

Built and ready for that verification, none of it proven against the live API:
OAuth callback bound by a single-use state token, client-side rate limiting
shared across processes, checksum-verified order postbacks with forward-only
reconciliation, daily session-expiry handling, instrument master sync, a
supervised Kite tick stream, and live quotes that refuse to price an order
rather than falling back to the simulator.

## API surface (summary)

All under `/api/v1`. Full schemas at `/docs`.

| Area | Endpoints |
|---|---|
| auth | `POST /auth/login` (returns a challenge), `POST /auth/login/verify` (TOTP or recovery code → session), `POST /auth/logout`, `GET /auth/me`, `GET /auth/invite/{token}`, `POST /auth/register` (invite-only), `GET /auth/sessions`, `DELETE /auth/sessions/{id}`, `POST /auth/totp/setup`, `POST /auth/totp/enable`, `GET /auth/totp/status`, `POST /auth/totp/recovery-codes`, `POST /auth/password/forgot`, `POST /auth/password/reset`, `POST /auth/password/change` |
| admin | `GET /admin/users`, `GET/POST /admin/invites`, `DELETE /admin/invites/{id}`, `POST /admin/users/{id}/suspend`, `POST .../unsuspend`, `POST .../reset-2fa`, `PATCH .../admin` |
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
| zerodha | Kite app; `<REF>_API_KEY` / `<REF>_API_SECRET` in backend env; redirect URL `http://localhost:8000/api/v1/brokers/zerodha/callback` | Connect mints a single-use state token → Kite login → callback verifies the state, exchanges request_token via SHA-256 checksum → token stored encrypted → land back on `/brokers?connected=…` → Verify read access. Token expires daily; Reconnect re-validates, Connect re-runs login. |
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

## Accounts and access

Registration is **invite-only**. There is no open signup: an operator invites
by email, the invitee clicks the emailed link and sets their own password.
Clicking the link proves control of the address, so invited accounts need no
separate email-verification step. Invites expire after 7 days and work once.

Bootstrapping a fresh deployment (no accounts exist yet, so nobody can invite):

```bash
make bootstrap-admin email=you@example.com
```

That creates or promotes an operator and prints a temporary password. Local dev
is already covered — `make seed` creates `demo@trader.local` as an operator.

On first sign-in every account, including a bootstrapped operator, is sent
through two-factor setup before it can reach anything else. Existing accounts
on an upgraded instance are treated the same way.

**Operator (`is_admin`)** gates actions whose blast radius crosses users: the
global kill switch, the `/admin` page, invites, and suspension. An operator
cannot suspend or demote themselves, and the last remaining operator cannot be
demoted by anyone — otherwise the instance becomes unadministrable and the
global kill switch unreachable.

**Two-factor authentication is mandatory.** Login is two-step: a correct
password returns a short-lived challenge, and only a TOTP code (or a recovery
code) exchanges that challenge for a session. A user who has not finished
enrolment can reach nothing but the setup flow — every other route refuses with
`totp_setup_required`, and the web app routes them to `/security/setup`.

Enrolment issues **ten single-use recovery codes**, shown exactly once. They
are the only way back in if an authenticator is lost, so they are hashed at
rest and cannot be redisplayed — generate a fresh set from `/security` if you
run low. An operator can also clear someone's second factor entirely
(`reset-2fa`), which revokes their sessions and sends them back to setup.

**Sessions are per-device.** `GET /auth/sessions` lists them, logout revokes
only the current one, and suspending an account revokes all of its sessions
immediately rather than waiting for tokens to expire.

**Password reset** is self-service: a single-use emailed link, valid 30
minutes. Completing a reset revokes every session, since a reset is the
response to a suspected compromise. Changing a password from `/security`
revokes every *other* session and keeps the current one.

**Login is rate limited** — 5 failures per 15 minutes, counted per email and
per IP. Only the email counter clears on success: clearing the IP counter too
would let anyone holding one valid login reset it at will. TOTP verification
and password-reset requests are limited the same way — a six-digit code needs
rate limiting to mean anything.

**Email** goes through Resend (`RESEND_API_KEY` + `EMAIL_FROM`). Without a key
configured, messages are written to the API log instead, so invite links stay
usable in local development.

### The broker OAuth callback

Kite redirects the user's browser to `/brokers/zerodha/callback`, which cannot
authenticate its caller — there is no session on that request, and the URL is
reachable by anyone. Binding a broker session to an account by an id in the
query string would therefore let a third party attach their own Kite session to
someone else's account, or a victim's session to their own, which is worse: the
victim's orders would route through credentials the attacker controls.

`POST /accounts/{id}/connect` mints a single-use state token that records the
user, account and broker it was issued for. The callback consumes it, resolves
the account **from the state** rather than the query string, and filters that
lookup by the user the state names. A callback with no state, an expired or
replayed state, or a state naming a different account is refused. States expire
in fifteen minutes and redeem exactly once.

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


## Phase 2: ledger, settlement and backtests

Apply migration **0008** (`make migrate`) before restarting API and workers.
It creates `cash_ledger`, `holdings`, `pending_settlements`, `candles`, and
`backtest_runs`. Existing paper cash carries forward from the latest funds
snapshot. Legacy positive CNC inventory waits one session from migration,
since it has no reliable settlement provenance; legacy CNC shorts must be
reset before upgrading. New CNC fills settle on the next NSE trading day.
Unknown calendar years fail explicitly, including late-2026 buys requiring
2027 settlement. The existing holiday-source verification caveat still applies.

Cash movements and inventory changes serialize on an account row lock. Ledger
updates/deletes are rejected by Postgres. Resets append offset and opening
entries, cancel working orders and pending settlements, and clear inventory;
order/fill history remains. Accounts with ledger history cannot be deleted;
disconnect them instead. `GET /portfolio/ledger?account_id=...` returns the
latest 100 entries; paginate with `before_id`. Paper funds on the dashboard,
portfolio, adapter and AI tool all read this ledger. Realized P&L includes
charges; cash includes both trade principal and charges.

The Positions page separates pending CNC positions from settled delivery
holdings. Settled inventory counts toward risk limits and strategy positions.
A delivery sale cannot consume unsettled stock. Settlement runs on worker
ticks and before fill attempts, using IST dates and T+1 sessions.

Strategies now evaluate completed candles (default `interval=1m`,
`source=simulator`), once per symbol/bar. Missing minutes stay missing; 5m
rollups require five distinct 1m candles. Price-only simulated ticks have
unknown volume. Raw tick SMA history is no longer the strategy input, so
new installations need enough completed candles to warm up.

Install the optional importer dependency and download history:

```bash
cd apps/api
uv pip install -e '.[dev,history]'
python -m app.cli.import_history --symbol RELIANCE --interval 1d \
  --start 2025-01-01 --end 2026-01-01
```

`--end` is exclusive; intervals are `1m`, `5m`, and `1d`. Imports validate the
whole download before writing, refuse malformed OHLCV/timestamps, skip
unfinished bars and upsert atomically. Yahoo and simulator rows have separate
source keys, so a reimport cannot overwrite simulated candles.
[yfinance](https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html)
is an unofficial Yahoo client; availability and intraday retention are limited
and requests can fail. Its documentation describes personal use. Review data
rights before offering imported data to others. We explicitly import
unadjusted OHLC; splits, dividends and other corporate actions are not modeled.

Open **Backtests** to run the SMA crossover against one symbol/source/range.
`POST /backtests` stores the configuration, candle-data hash, metrics and
curve; `GET /backtests` and `GET /backtests/{id}` return only your own runs.
Ranges are capped at 100,000 completed bars. A signal fills at the *next stored
bar's open*, never its own close. The final signal remains unfilled without a
next bar. Win rate counts closed round trips after charges. Open inventory is
marked to the last close. CNC uses the same settlement calendar and charges.

Limits are displayed with every result: long-only, no additional slippage,
market impact, liquidity or circuit breakers; no automatic MIS end-of-session
square-off; current charge rates across all historical dates; the paper plan's
zero brokerage. These results are a constrained simulator, not a broker P&L
reconciliation. AI/async strategies require recorded historical decisions and
are explicitly refused by the replay engine.

Postgres transaction tests are opt-in against a **disposable, migrated** DB:

```bash
cd apps/api
DEBUG=false TEST_DATABASE_URL=postgresql+asyncpg://user@localhost/test_db pytest -q
```

Without `TEST_DATABASE_URL`, these tests skip and the pure-unit suite runs.
History normalization tests require the optional history dependencies.
