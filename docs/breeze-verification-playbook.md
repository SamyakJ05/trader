# ICICI Breeze verification playbook

**Written for: the operator running this instance — you, with your own ICICI
Direct account and real money.**

Every Breeze call in this repo was written from ICICI's documentation and their
Python SDK's source, and has never touched their API. `adapter_status` is
`scaffold`, and the live gate refuses live orders structurally because of it.

This is the sequence that turns that into `working`. It ends with a real order
on your real account. Nobody can do that part for you.

Expect payload drift. Their docs and their SDK already disagree in places I
found while building this — the security master URL in the docs serves a
different file from the one in the SDK — so treat every response as something
to check rather than assume.

---

## How Breeze differs from Kite

If you have read the Zerodha playbook, these are the differences that matter.
They are not cosmetic.

**Stock codes, not trading symbols.** Breeze calls RELIANCE `RELIND`. This
platform stores each broker's own codes, so a strategy written for Breeze uses
`RELIND` and will not run on another broker, and vice versa. Strategies naming
symbols the broker does not recognise are refused at creation rather than
silently finding no data.

**No market orders.** Breeze's API accepts only `limit` and `stoploss`. Their
SDK fakes a market order by fetching a quote and computing an aggressive limit
price client-side; this adapter refuses instead, because that is a different
order from the one you asked for. Every order you place here is a limit order.

**No intraday cash product.** Breeze's products are `cash`, `mtf`, `btst`,
`futures`, `options` — there is no equivalent of Kite's MIS for cash equity.
MIS orders are refused rather than sent as delivery, which would turn an
intraday trade into one that settles and has to be funded.

**Sessions die at midnight IST**, not at a morning flush, and there is no
programmatic refresh — ICICI cite SEBI guidance for the daily reset. You log in
through a browser every day. A session issued at 11pm is dead an hour later.

**Every request is signed.** Kite uses a bearer token; Breeze needs a SHA-256
checksum over the timestamp, the exact serialized body, and your API secret.
Whitespace in the JSON changes the checksum. A mismatch produces an auth
failure that does not say "checksum" — it looks like a bad session.

**Tight rate limits.** 100 calls/minute and 5,000/day, per user, across all
endpoints. Exceeding them is documented as blocking the account rather than
returning something retryable. Both limits are enforced client-side before each
call.

---

## Before you start

**You need:**

- A Breeze app registered at <https://api.icicidirect.com/apiuser/home> —
  free, unlike Kite Connect
- `APP_ENCRYPTION_KEY` set, so session tokens are encrypted at rest

**The static IP, and what it does not block.** Registration asks for a Primary
IP Address, whitelisted under SEBI's algo framework (circular of 4 February
2025, universal from 1 April 2026). The requirement covers the transactional
layer only — placing, modifying, cancelling and squaring off. Market data, the
order book, positions and the websocket stream are reachable from any address,
and the daily browser login can be done anywhere: you copy the `API_Session`
from your browser and hand it to whichever host holds the registered IP.

So **stages 1 to 4 below run from your laptop today**. Only stage 5, the live
order, needs to originate from the registered address — typically a small
always-on VM with a reserved IP (ICICI publish setup guides for AWS, GCP and
Azure). You get one primary and one secondary IP, changeable about once a week,
so register something you intend to keep.

If an order is rejected while every read still works, suspect the IP before the
session or the payload. The adapter says so in that error, because suspicion
naturally falls on the other two first.

**Put your credentials in `.env` yourself:**

```bash
BREEZE_MAIN_API_KEY=your_app_key
BREEZE_MAIN_API_SECRET=your_secret_key
```

They stay in your environment — never in the database, never sent to the
frontend, and nobody helping with this repo needs to see them.

**Check your clock.** Breeze rejects requests whose timestamp differs from
their server's by more than 60 seconds. If your machine's clock drifts, every
call fails and the error will not mention time.

---

## Stage 1 — Connect

```bash
make up && make migrate && make seed
```

Sign in, complete 2FA setup, then Brokers → add an account:

- broker `icici_breeze`
- credential ref `BREEZE_MAIN` (must match the `.env` prefix exactly)
- environment `paper` — leave it there for now

Click **Connect**. You will be sent to ICICI's login page. After logging in,
**look at the address bar**: the redirect carries an `API_Session` value.

Unlike Zerodha, there is no automatic callback — copy that value and paste it
into **Set token** on the account card.

**What happens when you do:** the value you paste is not a session token. It is
exchanged via `/customerdetails` for the real session key, and that exchange is
also the only place your ICICI user id comes from. Both are stored; without the
user id nothing can be signed. If you see "Breeze session exchange failed", the
`API_Session` was already used, has expired, or the API secret is wrong.

---

## Stage 2 — Read paths

Click **Verify read access**. This calls `get_profile` and `get_funds` and
stamps `read_verified_at` only if both succeed.

Then exercise the rest:

```bash
make api-shell            # opens bash in the api container
python -m asyncio         # a REPL that can await
```

```python
from sqlalchemy import select
from app.db.session import async_session_factory
from app.adapters.registry import get_adapter
from app.db.models import BrokerAccount

async with async_session_factory() as db:
    account = (await db.execute(
        select(BrokerAccount).where(BrokerAccount.broker == "icici_breeze")
    )).scalar_one()
    adapter = get_adapter(account)

    print(await adapter.get_profile())
    print(await adapter.get_funds())
    print(await adapter.get_holdings())
    print(await adapter.get_positions())
    print(await adapter.get_orders())
```

**Check each result against your ICICI Direct dashboard.** Not "did it return
something" — does the funds figure match, are the holdings right, are the
quantities correct. A well-formed response full of zeroes has failed in the way
that matters.

Mind the rate budget while you do this: 5,000 calls a day sounds generous until
a sync loop spends it. The adapter warns in the logs when fewer than 100 remain.

When something is wrong, the fix belongs in
`app/adapters/icici_breeze/adapter.py` and nowhere else — that file is the only
place permitted to know Breeze's payload shapes.

---

## Stage 3 — Security master

This is what teaches the platform Breeze's stock codes. Nothing that names an
instrument works before it has run.

```python
from app.services.instruments import sync_instruments, token_map

async with async_session_factory() as db:
    account = ...  # as above
    print(await sync_instruments(db, account, exchange="NSE"))
    print(await token_map(db, broker="icici_breeze", symbols=["RELIND"]))
```

Expect tens of thousands of rows. Confirm a code you recognise: `RELIND` should
resolve, with "RELIANCE INDUSTRIES (RELIANCE)" as its name so the code is
legible to a human reading a position list.

**Note the URL.** Two security master zips are live and they are not mirrors —
the one in ICICI's SDK has no NSE equity file at all. This adapter uses the
docs' `NewSecurityMaster` URL, which does. If a sync ever returns nothing for
NSE, check that first.

---

## Stage 4 — Tick stream

Set the account's environment to `live` (this does **not** enable live orders —
that is a separate flag) and create a strategy naming Breeze codes.

```python
from app.workers.tick_stream import run_forever
await run_forever()      # runs until interrupted
```

**What good looks like:** `tick_stream_started`, then
`breeze_stream_subscribed` with an instrument count, then quiet. Ticks only
flow during market hours — 09:15–15:30 IST on a trading day. **Outside those
hours a working connection produces no ticks at all, which looks identical to a
broken one. Verify during market hours or you learn nothing.**

Check prices are landing:

```python
from app.core.redis import get_redis
from app.services.quotes import live_price

async with async_session_factory() as db:
    print(await live_price(db, get_redis(), symbol="RELIND"))
```

---

## Stage 5 — The live order

**This is the irreversible part.** Everything until now was read-only. This
spends money, and the exposure is real if the adapter is wrong in a way the
earlier stages did not reveal.

**This stage must run from the registered static IP.** The earlier ones did
not. If you have been working from a laptop, this is the point where the
platform moves to the host whose address you registered.

Do it during market hours, watching your ICICI dashboard at the same time.

### 5.1 Open the three gates

Each is a deliberate act:

1. `ENABLE_LIVE_TRADING=true` in `.env`, restart api and worker
2. `live_enabled=true` on the account row (Brokers page, "Enable live")
3. `CAPABILITY_MATRIX[Broker.ICICI_BREEZE].adapter_status = AdapterStatus.WORKING`
   in `app/domain/capabilities.py`

`tests/test_live_gate.py` fails once you make change 3. That is the tripwire
working: it exists so this flip cannot happen unnoticed in a diff. Update the
test in the same commit, saying what you verified and when.

### 5.2 Place one share, far from market

- 1 share of a liquid stock, by its **Breeze code**
- **LIMIT** — there is no other choice here, and that is deliberate
- price ~20% below market for a buy, so it rests in the book instead of filling
- product **CNC** (`cash`)

### 5.3 Check five things

1. **The order appears in your ICICI dashboard** with the same quantity, price
   and stock
2. **`broker_order_id` was recorded** on our row
3. **Audit shows the chain** — `ORDER_REQUESTED` → `RISK_CHECK` →
   `ORDER_SUBMITTED` → `BROKER_RESPONSE`
4. **The order did not duplicate.** Breeze's `user_remark` is a label, not an
   idempotency key — our own `client_order_id` uniqueness is the only guard, so
   this is worth confirming rather than assuming
5. **Charges** once it fills or cancels. Brokerage, STT and the rest appear on
   ICICI's contract note the next morning, and should match what the platform
   predicted — this instance is configured for Prime 999 (0.22% delivery,
   0.022% intraday), so check that is still your plan.

   **The DP charge will not be on the contract note.** It is debited from your
   ledger by the depository participant rather than billed with the trade, so
   a note showing no DP line is not evidence you were not charged ₹23.60.
   Check the ledger for it, not the note.

### 5.4 Cancel it

Confirm it disappears from ICICI and our row moves to `CANCELLED`.

---

## Stage 6 — Before anyone else uses this

Start with a hard quantity cap in a risk rule, not with a strategy you trust.

**SEBI algo registration.** Running algorithmic orders through a broker API is
regulated in India. Once anyone other than you runs a strategy here, you are
likely in territory requiring broker-approved algo registration. Raise it with
ICICI before you invite anyone.

**Brokerage rates.** ICICI's plan differs from Zerodha's and from the
placeholder this platform currently uses for Breeze. Until you correct it, every
paper P&L and backtest on a Breeze account is wrong in the same direction.

---

## What is still not verified even after all this

- **Partial fills.** A one-share order cannot partially fill.
- **Order modification.** Verify separately with another resting limit order.
- **Anything but CNC cash equity.** `mtf`, `btst`, futures and options are
  untested, and MIS is refused outright.
- **The order-notification stream.** Only the tick stream is consumed; order
  state comes from polling.
- **Reconnection under a real outage.** Tested against simulated failures only.

Say so when describing this platform's status. "Verified" should mean the path
you walked, not the whole adapter.
