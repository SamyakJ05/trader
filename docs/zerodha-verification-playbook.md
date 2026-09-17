# Zerodha verification playbook

**Written for: the operator running this instance — you, with your own Kite
account and real money.**

Every Kite call in this repo was written from Zerodha's documentation and has
never touched their API. That is the honest status: `adapter_status` is
`scaffold`, and the live gate refuses live orders structurally because of it.

This is the sequence that turns that into `working`. It ends with a real order
for real money on a real account. Nobody can do that part for you, and no test
in this repo substitutes for it.

Expect payload drift. Code written against docs and never run against the API
is usually wrong somewhere — a renamed field, a string where a number was
expected, a wrapper object. Drift is the normal outcome of this exercise, not a
sign something went badly.

---

## Before you start

**You need:**

- A Kite Connect app at <https://developers.kite.trade> — ₹2,000/month, billed
  to you. The free personal API does not include the endpoints this uses.
- The redirect URL on that app set to
  `http://localhost:8000/api/v1/brokers/zerodha/callback`
- The postback URL set to `https://<your-host>/api/v1/webhooks/zerodha`, if you
  want order updates pushed. Kite will not post to localhost.
- `APP_ENCRYPTION_KEY` set. Session tokens are stored encrypted and 2FA setup
  refuses without it.

**Put your credentials in `.env` yourself:**

```bash
ZERODHA_MAIN_API_KEY=your_api_key
ZERODHA_MAIN_API_SECRET=your_api_secret
```

These stay in your environment. They are never written to the database, never
sent to the frontend, and nobody helping you with this repo needs to see them.

**A note on what you are agreeing to.** Once an adapter is `working` and an
account is `live_enabled`, this software can place orders that spend your
money. The gates exist because that should be a deliberate act, taken once, by
someone who has read what the code does.

---

## Stage 1 — Connect

```bash
make up && make migrate && make seed
```

Sign in, complete 2FA setup, then Brokers → add an account:

- broker `zerodha`
- credential ref `ZERODHA_MAIN` (must match the `.env` prefix exactly)
- environment `paper` — leave it there for now

Click **Connect**. You should land on Kite's login, then come back to
`/brokers?connected=<id>`.

**What is being tested:** the OAuth state token. Connect mints a single-use
state; the callback consumes it and resolves the account from the state rather
than the URL. If you see `?error=invalid_state`, the state expired (15 minutes)
or was already used — start the connect again rather than re-opening an old
tab.

**Likely failures**

| Symptom | Cause |
|---|---|
| `Missing ZERODHA api key` | `.env` prefix does not match `credential_ref` |
| Kite rejects the redirect | Redirect URL on the Kite app does not match exactly, including port |
| `error=token_exchange_failed` | API secret wrong, or the request token was already spent |

---

## Stage 2 — Read paths

Click **Verify read access**. This calls `get_profile` and `get_funds` and
stamps `read_verified_at` only if both succeed.

Then exercise the rest, which `verify` does not cover:

```bash
make api-shell            # opens bash in the api container
python -m asyncio         # a REPL that can await
```

```python
from app.db.session import async_session_factory
from app.adapters.registry import get_adapter
from app.db.models import BrokerAccount
from sqlalchemy import select

async with async_session_factory() as db:
    account = (await db.execute(
        select(BrokerAccount).where(BrokerAccount.broker == "zerodha")
    )).scalar_one()
    adapter = get_adapter(account)

    print(await adapter.get_profile())
    print(await adapter.get_funds())
    print(await adapter.get_holdings())
    print(await adapter.get_positions())
    print(await adapter.get_orders())
```

**Read each result against your Kite web dashboard.** Not "did it return
something" — does the funds figure match, do the holdings match, are the
quantities right. A call that returns a well-formed object full of zeroes has
failed in the way that matters.

Every one of these is a candidate for drift. When something is wrong, the fix
is in `app/adapters/zerodha/adapter.py` and nowhere else — that file is the
only place in the codebase permitted to know Kite's payload shapes.

**Capture what you find.** A short note per endpoint — expected field, actual
field — makes the fixes mechanical instead of exploratory.

---

## Stage 3 — Instruments

```python
from app.services.instruments import sync_instruments, token_map

async with async_session_factory() as db:
    account = ...  # as above
    print(await sync_instruments(db, account, exchange="NSE"))
    print(await token_map(db, broker="zerodha", symbols=["RELIANCE", "INFY"]))
```

Expect tens of thousands of rows for NSE, and a token map with a numeric token
per symbol.

**If `token_map` returns fewer symbols than you asked for**, those symbols have
no instrument token and the tick feed will silently not subscribe to them. That
is deliberate — inventing a token would deliver another instrument's prices
under a name you trust — but it means a strategy on that symbol gets no data.

---

## Stage 4 — Tick stream

Set the account's environment to `live` (this does **not** enable live orders —
that is a separate flag) and make sure a strategy exists naming symbols you
want prices for.

Start the stream and watch the logs (in `make api-shell`, `python -m asyncio`):

```python
from app.workers.tick_stream import run_forever
await run_forever()      # runs until interrupted
```

**What good looks like:** `tick_stream_started`, then
`kite_ticker_connected` with an instrument count, then quiet. Ticks only flow
during market hours — 09:15–15:30 IST on a trading day. Outside those hours a
successful connection produces no ticks at all, which is correct and looks
identical to a broken one. **Verify this during market hours or you learn
nothing.**

Check prices are actually landing:

```python
from app.core.redis import get_redis
from app.services.quotes import live_price

async with async_session_factory() as db:
    print(await live_price(db, get_redis(), symbol="RELIANCE"))
```

**Likely failures**

| Log line | Meaning |
|---|---|
| `tick_stream_no_tokens` | Instruments not synced, or symbols absent from the master |
| `tick_stream_idle` | No strategy on this account names any symbol |
| `kite_ticker_gave_up_reconnecting` | Session dead or network blocked; the platform is now running blind |

---

## Stage 5 — The live order

**This is the irreversible part.** Everything until now was read-only. This
stage spends money, and the exposure is real if the adapter is wrong in a way
the earlier stages did not reveal.

Do this during market hours, when you can watch Kite's own dashboard at the
same time.

### 5.1 Open the three gates

They are separate on purpose — each is a deliberate act:

1. `ENABLE_LIVE_TRADING=true` in `.env`, restart api and worker
2. `live_enabled=true` on the account row (Brokers page, "Enable live")
3. `CAPABILITY_MATRIX[Broker.ZERODHA].adapter_status = AdapterStatus.WORKING`
   in `app/domain/capabilities.py`

`tests/test_live_gate.py` fails once you make change 3. That is the tripwire
working as designed: it exists so this flip cannot happen accidentally or
unnoticed in a diff. Update the test in the same commit, with a message saying
what you verified and when.

### 5.2 Place one share, far from market

A limit order well away from the current price, so it rests in the book instead
of filling:

- 1 share of a liquid stock
- **LIMIT**, not MARKET — a market order fills immediately and you lose the
  chance to inspect it before it is real
- price a few percent below market for a buy — far enough to rest in the
  book rather than fill, close enough to stay inside the exchange's daily
  price band. **Not 20%**: NSE bands most scrips at 10% (tighter for some),
  and a price outside the band is rejected outright — "Price entered by you
  is beyond the price range permitted by exchange". 5-7% below is the range
  that works; RELIND at 1244 was accepted at 1170 and refused at 995.
- product **CNC**

Place it from the Orders page.

### 5.3 Check four things

1. **The order appears in Kite's own dashboard** with the same quantity, price
   and symbol
2. **`broker_order_id` was recorded** on our row
3. **Audit shows the full chain** — `ORDER_REQUESTED` → `RISK_CHECK` →
   `ORDER_SUBMITTED` → `BROKER_RESPONSE`
4. **Charges look right** once it fills or is cancelled — compare against
   Zerodha's own contract note when it arrives the next morning

### 5.4 Cancel it

From the Orders page. Confirm it disappears from Kite, and that our row moves
to `CANCELLED`.

### 5.5 Postbacks

If you configured a postback URL on a public host, the cancel should have
arrived as a webhook. Check the audit log for a state change `via:
zerodha_postback`.

A postback whose checksum does not verify is refused and logged as
`postback_refused` — if you see that for a genuine postback, the API secret in
`.env` disagrees with the Kite app's.

---

## Stage 6 — Before anyone else uses this

**Then, and only then, consider a strategy placing live orders.** Start with a
hard quantity cap in a risk rule, not with a strategy you trust.

Two things that are not code problems and cannot be solved by testing:

**SEBI algo registration.** Running algorithmic orders through a broker API is
regulated in India. Once anyone other than you runs a strategy on this
instance, you are likely operating in territory that requires broker-approved
algo registration. Raise it with Zerodha before you invite anyone — the answer
shapes what you are allowed to offer.

**The charge rates will drift.** They are dated configuration in
`app/engines/paper/rates.py`, verified against NSE circulars at the time of
writing. Budgets and circulars change them. When a real contract note disagrees
with what the platform predicted, the contract note is right — update the rates
and add the new period to the table rather than adjusting the current one.

---

## What is still not verified even after all this

- **Partial fills on a live order.** A 1-share order cannot partially fill. The
  partial-fill path stays untested against the real broker.
- **Order modification.** `modify_order` is scaffold. Verify it separately with
  another resting limit order if you intend to use it.
- **Anything but CNC equity.** MIS, F&O, and every other product and segment
  are untested.
- **Reconnection under a real outage.** Tested against simulated failures only.

Say so when describing this platform's status to anyone. "Verified" should mean
the specific path you walked, not the whole adapter.
