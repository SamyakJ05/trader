# Deploying to DigitalOcean

**Written for: the operator doing this deployment — you.**

Domain: `tickortrade.online`, already registered.

This takes about an hour, most of it waiting for DNS and the managed database
to provision. Work through it in order; the later steps assume the earlier ones.

**Do not register the static IP with ICICI until step 9.** The reserved IP has
to exist and be attached first, and ICICI allow changing it only about once a
week — so registering an address you then change costs you days.

---

## What this costs

| | | Monthly |
|---|---|---|
| Droplet, Basic 2 vCPU / 4 GB / 80 GB | runs everything except the database | $24.00 |
| Managed Postgres, 1 GB / 1 vCPU / 10 GiB | the part that must not be lost | $15.15 |
| Reserved IP | free while attached to a running droplet | $0 |
| **Total** | | **$39.15** |

The tier below is $18 for 2 vCPU / 2 GB — the same two cores, six dollars less,
and half the RAM. RAM is the constraint that decides it. Five containers run
here (api with 2 workers, worker, web, Redis, Caddy) and `next build` is the
memory-hungry step; building on the droplet at 2 GB meets the OOM killer.

Premium Intel and Premium AMD are offered as toggles on the same Basic plans
for a few dollars more, buying NVMe storage and newer CPUs. Not worth it here:
this workload waits on Postgres round trips and broker HTTP, not on local disk.

Transfer is 4 TB outbound on this tier, against an instance serving a handful
of users with no media. It will not be close.

Managed Postgres rather than a container is the one place worth paying for:
it holds the audit log, the cash ledger and every fill, which is the only
thing here that cannot be rebuilt from git.

---

## 1. Create the droplet

DigitalOcean → Droplets → Create.

- **Image:** Ubuntu 24.04 LTS
- **Plan:** Basic → Regular → **2 vCPU / 4 GB / 80 GB** ($24/mo)
- **Region:** **Bangalore (BLR1)**. Latency to NSE matters less than you would
  think for this workload, but the managed database must be in the same region
  as the droplet or you pay for — and wait on — cross-region traffic on every
  query.
- **Authentication:** SSH key, not a password.
- **Hostname:** `tickortrade`

Under Advanced, leave monitoring enabled — it is free and gives you the CPU
and memory graphs you will want when something is slow.

---

## 2. Reserved IP

Networking → Reserved IPs → assign a new one to the droplet.

**Do this before anything else touches the address.** A droplet's default
public IP does not survive a rebuild, and the whole point of the ICICI
registration is that the address is stable.

Note it down. This is the address that goes in DNS, in `BROKER_STATIC_IP`,
and eventually into ICICI's registration form.

---

## 3. DNS

At your registrar, point the domain at the reserved IP:

| Type | Name | Value |
|---|---|---|
| A | `@` | your reserved IP |
| A | `www` | your reserved IP |

Then wait. Caddy cannot obtain a certificate until the domain resolves to the
droplet, and a failed attempt counts against Let's Encrypt's rate limit.

```bash
dig +short tickortrade.online     # must return your reserved IP before step 7
```

---

## 4. Managed Postgres

Databases → Create → PostgreSQL 16, **same region as the droplet (BLR1)**,
smallest single-node plan ($15.15/mo).

Once it is up:

1. **Settings → Trusted Sources → add the droplet.** Until you do, the
   database accepts connections from any address with the password. This is
   the single most important checkbox on the page.
2. **Users & Databases** → create a database named `trader`.
3. Copy the connection string. It looks like:
   `postgresql://doadmin:...@db-....ondigitalocean.com:25060/trader?sslmode=require`

The application uses asyncpg, which needs a different scheme and does not
accept `sslmode` as a query parameter:

```
postgresql+asyncpg://doadmin:PASSWORD@HOST:25060/trader
```

Drop `?sslmode=require` from the URL. DigitalOcean requires TLS at the server,
and asyncpg negotiates it; leaving the parameter in causes a connection error
that does not mention TLS.

**Check the connection limit** under the cluster's Overview once it exists.
`DB_POOL_SIZE` × (api workers + 1) must stay under it — the defaults of 3 + 2
hold two api workers and one arq worker to 15 at saturation, which is safe for
any plan, but raise them only against a number you have actually read.

---

## 5. Harden the droplet

```bash
ssh root@YOUR_RESERVED_IP
```

```bash
# Updates first.
apt update && apt upgrade -y

# A non-root user to run the stack.
adduser --disabled-password --gecos "" deploy
usermod -aG sudo deploy
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy/

# Firewall: SSH and the web. Nothing else is reachable — the api, web and
# Redis containers publish no host ports at all, and the database is managed.
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# Unattended security updates.
apt install -y unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades
```

Then disable root SSH and password login:

```bash
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh
```

**Open a second terminal and confirm `ssh deploy@YOUR_IP` works before closing
this one.** Getting this wrong locks you out of your own droplet.

---

## 6. Docker

As `deploy`:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker deploy
newgrp docker          # or log out and back in
docker compose version # confirm the plugin is present
```

---

## 7. The application

```bash
git clone https://github.com/YOUR_USER/trader.git
cd trader
cp .env.example .env
```

Now fill in `.env`. **These are your secrets — nobody else needs to see them,
and they never enter the database or reach the browser.**

```bash
# Generate the two keys. Run each and paste the output.
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
openssl rand -hex 32
```

The settings that must change from their defaults:

| Setting | Value |
|---|---|
| `DOMAIN` | `tickortrade.online` |
| `PUBLIC_API_URL` | `https://tickortrade.online/api/v1` — the suffix matters; paths are appended to it directly |
| `DATABASE_URL` | the asyncpg URL from step 4 |
| `APP_ENCRYPTION_KEY` | the Fernet key |
| `APP_SECRET_KEY` | the openssl output |
| `CORS_ORIGINS` | `["https://tickortrade.online"]` |
| `WEB_BASE_URL` | `https://tickortrade.online` |
| `DEBUG` | `false` |
| `MARKET_HOURS_ENFORCED` | `true` |
| `ENABLE_LIVE_TRADING` | `false` — leave it |
| `BROKER_STATIC_IP` | your reserved IP |
| `RESEND_API_KEY` | for invite and reset emails |
| `EMAIL_FROM` | a verified sender on your Resend domain |

Leave `BROKER_CREDENTIAL_OWNERS` empty for now. It comes into play in step 9.

```bash
chmod 600 .env
```

Bring it up:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api alembic upgrade head
```

The first start takes a few minutes: two images build, and Caddy obtains a
certificate. Watch it:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f caddy
```

`certificate obtained successfully` means DNS and TLS are both right.

---

## 8. Verify before trusting it

```bash
curl -fsS https://tickortrade.online/api/v1/readyz | jq
```

Expect `"status": "ok"` with `db`, `redis` and `worker` all true. If `worker`
is false the arq container did not start — `docker compose ... logs worker`.

Check the startup log for the things that fail quietly:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs api | grep -E "config_|egress_ip"
```

- Any `config_` line at error level is a setting still wrong. Fix it.
- `egress_ip` should show `detected` equal to your reserved IP. If it shows
  `egress_ip_mismatch`, the droplet is not leaving from the address you think
  it is, and step 9 would register the wrong one.

Then create your operator account — registration is invite-only, so the first
one is made from the command line:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  exec api python -m app.seeds.bootstrap_admin you@example.com
```

Sign in at `https://tickortrade.online`, complete TOTP enrolment, and **save
your recovery codes somewhere that is not the droplet**.

Run the integration tests, which cover concurrent fills and the ledger
invariants and were skipped everywhere a Postgres was unavailable:

```bash
make test-integration
```

---

## 9. Only now: ICICI

The platform is running, TLS works, the outbound IP is confirmed and stable.

1. **Register at <https://api.icicidirect.com/apiuser/home>.** The Primary IP
   Address is your reserved IP.
2. Put the credentials in `.env` yourself:
   ```
   BREEZE_MAIN_API_KEY=...
   BREEZE_MAIN_API_SECRET=...
   BROKER_CREDENTIAL_OWNERS=BREEZE_MAIN:you@example.com
   ```
   `BROKER_CREDENTIAL_OWNERS` is what binds that credential set to your
   account. Without it the ref cannot be attached at all; with the wrong email
   another user could attach it and read your real broker account.
3. Restart api and worker so they pick up the new environment.
4. Follow `docs/breeze-verification-playbook.md` from stage 1. It ends with a
   single real order, placed by you.

**`ENABLE_LIVE_TRADING` stays false until that playbook says otherwise**, and
the adapter stays `scaffold` until you have verified it against the real API
yourself. `tests/test_live_gate.py` fails the moment that flips, which is the
tripwire working.

---

## 10. Backups

```bash
DATABASE_URL="postgresql://doadmin:...@...:25060/trader?sslmode=require" make backup
DATABASE_URL="..." make verify-restore file=backup-$(date +%F).dump
```

Note the plain `postgresql://` scheme with `sslmode=require` here — `pg_dump`
wants the standard URL, unlike the application's asyncpg one.

Do this monthly. Managed Postgres already takes daily backups with a week of
point-in-time recovery; these are the longer-lived copies, and running
`verify-restore` is what tells you they are real.

**Back up `.env` off the droplet** — a password manager is fine. It holds
`APP_ENCRYPTION_KEY`, and without that key every stored TOTP secret and broker
session token is unreadable.

---

## 11. Watch it

Point a free uptime monitor (UptimeRobot, Better Stack, Healthchecks.io) at:

```
https://tickortrade.online/api/v1/readyz
```

every minute, alerting on any non-200. That single check covers Postgres,
Redis and the worker — and the worker is the one whose death is otherwise
invisible, because every page keeps rendering while nothing fills.

`docs/operations.md` has the rest: log events worth knowing by name, what to
do when orders are rejected while reads still work, and how to stop trading in
a hurry.

---

## Deploying a change, afterwards

```bash
cd trader && git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec api alembic upgrade head
curl -fsS https://tickortrade.online/api/v1/readyz | jq
```

Check `/readyz` before walking away.
