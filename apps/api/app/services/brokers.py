"""Broker account service: connection lifecycle and data sync."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import BrokerError, FeatureNotSupportedError, SessionExpiredError
from app.adapters.registry import get_adapter
from app.core.config import BrokerEnvCredentials, get_settings
from app.core.security import encrypt_secret
from app.db.models import BrokerAccount, FundsSnapshot, HoldingsSnapshot
from app.domain.capabilities import get_capabilities
from app.domain.enums import AdapterStatus, AuditEventType, Broker, BrokerAccountStatus
from app.services import audit


async def get_account(
    db: AsyncSession, user_id: uuid.UUID, account_id: uuid.UUID
) -> BrokerAccount | None:
    result = await db.execute(
        select(BrokerAccount).where(
            BrokerAccount.id == account_id, BrokerAccount.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def connect(db: AsyncSession, account: BrokerAccount) -> dict:
    adapter = get_adapter(account)
    result = await adapter.connect()
    if result.get("status") == "connected":
        account.status = BrokerAccountStatus.CONNECTED.value
        account.status_message = None
    else:
        account.status = BrokerAccountStatus.PENDING_AUTH.value
    await audit.emit(
        db,
        AuditEventType.BROKER_SESSION,
        user_id=account.user_id,
        entity_type="broker_account",
        entity_id=account.id,
        payload={"action": "connect", "flow": result.get("flow")},
    )
    await db.commit()
    return result


async def store_session_token(
    db: AsyncSession, account: BrokerAccount, token: str, expires_at: datetime | None = None
) -> None:
    account.session_token_enc = encrypt_secret(token)
    account.session_expires_at = expires_at
    account.status = BrokerAccountStatus.CONNECTED.value
    account.status_message = None
    await audit.emit(
        db,
        AuditEventType.BROKER_SESSION,
        user_id=account.user_id,
        entity_type="broker_account",
        entity_id=account.id,
        payload={"action": "session_stored", "broker": account.broker},
    )
    await db.commit()


async def refresh_session(db: AsyncSession, account: BrokerAccount) -> dict:
    adapter = get_adapter(account)
    try:
        result = await adapter.refresh_session()
        account.status = BrokerAccountStatus.CONNECTED.value
        account.status_message = None
    except SessionExpiredError as e:
        account.status = BrokerAccountStatus.SESSION_EXPIRED.value
        account.status_message = str(e)
        result = {"status": "session_expired", "detail": str(e)}
    except BrokerError as e:
        account.status = BrokerAccountStatus.ERROR.value
        account.status_message = str(e)
        result = {"status": "error", "detail": str(e)}
    await audit.emit(
        db,
        AuditEventType.BROKER_SESSION,
        user_id=account.user_id,
        entity_type="broker_account",
        entity_id=account.id,
        payload={"action": "refresh", "result": result.get("status")},
    )
    await db.commit()
    return result


def credential_status(account: BrokerAccount) -> dict:
    """Masked credential metadata for the frontend. NEVER includes secret
    material — only whether things are configured and when they expire.

    The booleans are still an observation about the process environment, so
    they are only meaningful for a ref the account's owner was entitled to
    attach. Refs are validated and ownership-checked at creation, which is
    what keeps this from being a probe for which credentials the instance
    holds.
    """
    env_creds = BrokerEnvCredentials(account.credential_ref or "")
    return {
        "env_keys_configured": env_creds.has_api_keys,
        "env_access_token_configured": bool(env_creds.access_token),
        "session_token_configured": bool(account.session_token_enc),
        "session_expires_at": (
            account.session_expires_at.isoformat() if account.session_expires_at else None
        ),
    }


def can_enable_live(account: BrokerAccount) -> str | None:
    """Refusal reason for flipping live_enabled on, or None if permitted.
    Mirrors the order pipeline's triple gate so the UI cannot promise what
    dispatch would refuse."""
    if account.broker == Broker.PAPER.value:
        return "Paper accounts cannot be live"
    if not get_settings().enable_live_trading:
        return "ENABLE_LIVE_TRADING is false (global gate)"
    capabilities = get_capabilities(Broker(account.broker))
    if capabilities.adapter_status != AdapterStatus.WORKING:
        return (
            f"{account.broker} adapter status is '{capabilities.adapter_status}' — "
            "verify the adapter before enabling live"
        )
    if account.read_verified_at is None:
        return "Read access has never been verified for this account"
    return None


async def verify_read_access(db: AsyncSession, account: BrokerAccount) -> dict:
    """Exercise the broker read path (profile + funds). Success stamps
    read_verified_at; failure surfaces per-check errors without guessing."""
    adapter = get_adapter(account)
    checks: dict[str, str] = {}
    ok = True
    for name, call in (("profile", adapter.get_profile), ("funds", adapter.get_funds)):
        try:
            await call()
            checks[name] = "ok"
        except FeatureNotSupportedError as e:
            checks[name] = f"skipped: {e}"
        except SessionExpiredError as e:
            ok = False
            checks[name] = f"session_expired: {e}"
            account.status = BrokerAccountStatus.SESSION_EXPIRED.value
            account.status_message = str(e)
        except BrokerError as e:
            ok = False
            checks[name] = f"error: {e}"
            account.status = BrokerAccountStatus.ERROR.value
            account.status_message = str(e)

    if ok:
        account.read_verified_at = datetime.now(timezone.utc)
        account.status = BrokerAccountStatus.CONNECTED.value
        account.status_message = None
    await audit.emit(
        db,
        AuditEventType.BROKER_SESSION,
        user_id=account.user_id,
        entity_type="broker_account",
        entity_id=account.id,
        payload={"action": "verify_read_access", "verified": ok, "checks": checks},
    )
    await db.commit()
    return {"verified": ok, "checks": checks}


async def disconnect(db: AsyncSession, account: BrokerAccount) -> None:
    """Drop the stored broker session without deleting the account record."""
    account.session_token_enc = None
    account.session_expires_at = None
    account.status = BrokerAccountStatus.DISCONNECTED.value
    account.status_message = None
    await audit.emit(
        db,
        AuditEventType.BROKER_SESSION,
        user_id=account.user_id,
        entity_type="broker_account",
        entity_id=account.id,
        payload={"action": "disconnect", "broker": account.broker},
    )
    await db.commit()


async def sync_snapshots(db: AsyncSession, account: BrokerAccount) -> dict:
    """Pull funds/holdings from the broker into snapshot tables. Skips
    unsupported features per capability flags instead of failing."""
    adapter = get_adapter(account)
    synced: dict = {"funds": False, "holdings": False, "skipped": []}

    if account.broker == Broker.PAPER.value:
        funds = await adapter.get_funds()
        db.add(
            FundsSnapshot(
                broker_account_id=account.id,
                available_cash=funds.available_cash,
                payload=audit.jsonable(funds.raw),
            )
        )
        synced["funds"] = True
        synced["skipped"].append("holdings (paper has positions only)")
    else:
        try:
            funds = await adapter.get_funds()
            db.add(
                FundsSnapshot(
                    broker_account_id=account.id,
                    available_cash=funds.available_cash,
                    payload=audit.jsonable(funds.raw),
                )
            )
            synced["funds"] = True
        except (BrokerError, FeatureNotSupportedError) as e:
            synced["skipped"].append(f"funds: {e}")
        try:
            holdings = await adapter.get_holdings()
            db.add(
                HoldingsSnapshot(
                    broker_account_id=account.id,
                    holdings=audit.jsonable([h.model_dump() for h in holdings]),
                )
            )
            synced["holdings"] = True
        except (BrokerError, FeatureNotSupportedError) as e:
            synced["skipped"].append(f"holdings: {e}")

        # Positions were never synced at all -- only funds and holdings were,
        # so the Positions page showed nothing for a live account no matter
        # what the broker reported. Holdings are shares settled in demat;
        # positions are open intraday and F&O exposure, and they are the ones
        # a strategy sizes against.
        try:
            synced["positions"] = await _sync_positions(db, account, adapter)
        except (BrokerError, FeatureNotSupportedError) as e:
            synced["skipped"].append(f"positions: {e}")

    account.last_sync_at = datetime.now(timezone.utc)
    await audit.emit(
        db,
        AuditEventType.BROKER_SYNC,
        user_id=account.user_id,
        entity_type="broker_account",
        entity_id=account.id,
        payload=synced,
    )
    await db.commit()
    return synced


async def _sync_positions(db: AsyncSession, account: BrokerAccount, adapter) -> int:
    """Mirror the broker's open positions into our Position rows.

    The broker is the source of truth here, not our fill history: a position
    opened outside this platform, or one whose fill we missed, still exists at
    the broker and a strategy must size against reality rather than against
    what we happened to record.

    Rows the broker no longer reports are zeroed rather than deleted, because
    realized_pnl on them is part of the day's accounting and deleting it would
    quietly change the numbers.
    """
    from app.db.models import Position

    positions = await adapter.get_positions()
    seen: set[tuple[str, str, str]] = set()

    for reported in positions:
        key = (reported.symbol, reported.exchange.value, reported.product.value)
        seen.add(key)
        existing = (
            await db.execute(
                select(Position).where(
                    Position.broker_account_id == account.id,
                    Position.symbol == reported.symbol,
                    Position.exchange == reported.exchange.value,
                    Position.product == reported.product.value,
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            existing = Position(
                user_id=account.user_id,
                broker_account_id=account.id,
                environment=account.environment,
                symbol=reported.symbol,
                exchange=reported.exchange.value,
                product=reported.product.value,
            )
            db.add(existing)

        existing.quantity = reported.quantity
        existing.average_price = reported.average_price
        if reported.last_price is not None:
            existing.last_price = reported.last_price
        if reported.realized_pnl is not None:
            existing.realized_pnl = reported.realized_pnl

    # Anything we hold a row for that the broker no longer reports is closed.
    stale = (
        await db.execute(
            select(Position).where(
                Position.broker_account_id == account.id,
                Position.quantity != 0,
            )
        )
    ).scalars()
    for row in stale:
        if (row.symbol, row.exchange, row.product) not in seen:
            row.quantity = 0

    return len(positions)


async def diagnostics(db: AsyncSession, account: BrokerAccount) -> dict:
    """Exercise every read path and report what each returned.

    verify_read_access covers profile and funds, which is what read_verified_at
    means and all it should mean. But the verification playbooks then ask the
    operator to open an async REPL and call get_holdings, get_positions and
    get_orders by hand, and to check that the strategy's symbols actually
    resolve to instrument tokens. That is a reading task -- compare our numbers
    against the broker's own dashboard -- not a shell task, and it is repeated
    per broker and after every adapter change.

    Deliberately read-only and deliberately NOT stamping read_verified_at: a
    panel the operator can run at any time must not be able to promote an
    account's verification state as a side effect.

    Every call is caught individually. One unsupported or failing read must
    still leave the others visible, because the useful signal is usually which
    ones differ.
    """
    adapter = get_adapter(account)
    reads: dict[str, dict] = {}

    async def run(name: str, call, summarise):
        try:
            value = await call()
        except FeatureNotSupportedError as exc:
            reads[name] = {"status": "unsupported", "detail": str(exc)}
        except SessionExpiredError as exc:
            reads[name] = {"status": "session_expired", "detail": str(exc)}
        except BrokerError as exc:
            reads[name] = {"status": "error", "detail": str(exc)}
        except Exception as exc:
            # Broad on purpose: a diagnostic that raises tells the operator
            # nothing, and an adapter bug is exactly what this is for finding.
            reads[name] = {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
        else:
            reads[name] = {"status": "ok", **summarise(value)}

    await run(
        "profile",
        adapter.get_profile,
        lambda p: {"client_id": p.broker_client_id or None, "name": p.name},
    )
    await run(
        "funds",
        adapter.get_funds,
        lambda f: {
            "available_cash": str(f.available_cash),
            "margin_used": str(f.margin_used) if f.margin_used is not None else None,
        },
    )
    await run(
        "holdings",
        adapter.get_holdings,
        lambda rows: {
            "count": len(rows),
            # The sellable figure and the total differ when stock is pledged
            # or locked, and sizing a sell off the total is the mistake this
            # makes visible.
            "sample": [
                {
                    "symbol": h.symbol,
                    "quantity": h.quantity,
                    "total_quantity": h.total_quantity,
                    "average_price": str(h.average_price)
                    if h.average_price is not None
                    else None,
                }
                for h in rows[:5]
            ],
        },
    )
    await run(
        "positions",
        adapter.get_positions,
        lambda rows: {
            "count": len(rows),
            "sample": [
                {
                    "symbol": p.symbol,
                    "product": p.product.value,
                    "quantity": p.quantity,
                    "average_price": str(p.average_price),
                }
                for p in rows[:5]
            ],
        },
    )
    await run(
        "orders",
        adapter.get_orders,
        lambda rows: {
            "count": len(rows),
            "sample": [
                {
                    "broker_order_id": o.broker_order_id,
                    "symbol": o.symbol,
                    "status": o.status.value,
                    "quantity": o.quantity,
                    "filled_quantity": o.filled_quantity,
                }
                for o in rows[:5]
            ],
        },
    )

    return {
        "broker": account.broker,
        "environment": account.environment,
        "status": account.status,
        "reads": reads,
        "instrument_coverage": await _instrument_coverage(db, account),
    }


async def _instrument_coverage(db: AsyncSession, account: BrokerAccount) -> dict:
    """Which of this account's strategy symbols resolve to a broker token.

    The tick stream subscribes by token, so a symbol with none receives no
    prices -- and the runner then refuses to trade it live for want of a fresh
    quote. Nothing errors; the strategy simply sits silent. The playbook flags
    this as the failure that is hardest to notice, which is exactly why it
    belongs in a panel rather than a REPL session.
    """
    from app.db.models import Strategy
    from app.services import instruments as instrument_service

    result = await db.execute(
        select(Strategy.symbols).where(
            Strategy.broker_account_id == account.id,
            Strategy.status.in_(["RUNNING", "DRAFT"]),
        )
    )
    symbols: set[str] = set()
    for row in result.scalars():
        symbols.update(row or [])
    if not symbols:
        return {"symbols": 0, "resolved": 0, "missing": []}

    tokens = await instrument_service.token_map(
        db, broker=account.broker, symbols=sorted(symbols)
    )
    resolved = set(tokens.values())
    missing = sorted(symbols - resolved)
    return {
        "symbols": len(symbols),
        "resolved": len(symbols) - len(missing),
        "missing": missing,
    }


async def trading_readiness(db: AsyncSession, user_id) -> dict:
    """Everything that must be true before strategies trade unattended today.

    The operator's daily routine is a browser login before the open -- Breeze
    sessions die at midnight IST and ICICI publish no way to renew one
    programmatically. After that login nobody is watching, so the question
    "is it actually ready?" has to be answerable in one place rather than by
    checking the brokers page, the strategies page, the risk page and the
    logs separately.

    Each check reports pass/fail and, when it fails, what to do about it.
    Ordered by what blocks first: no session means nothing else matters.
    """
    # get_settings is imported at module scope. Re-importing it here shadowed
    # that binding, which made the setting unpatchable from a test -- a real
    # hazard for a check that reports whether real money may move. The rest
    # are local only because the module does not otherwise need them.
    from app.core.redis import get_redis
    from app.db.models import Strategy
    from app.domain.calendar import IST
    from app.domain.enums import Environment, StrategyStatus
    from app.services import killswitch

    settings = get_settings()
    checks: list[dict] = []

    accounts = (
        await db.execute(
            select(BrokerAccount).where(
                BrokerAccount.user_id == user_id,
                BrokerAccount.environment == Environment.LIVE.value,
            )
        )
    ).scalars().all()

    def add(name: str, ok: bool, detail: str, fix: str | None = None) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "fix": fix})

    # 1. An account at all.
    if not accounts:
        add(
            "broker account",
            False,
            "No live broker account exists.",
            "Connect one on the Brokers page.",
        )
        return {"ready": False, "checks": checks}
    add("broker account", True, f"{len(accounts)} live account(s).")

    connected = [
        a for a in accounts if a.status == BrokerAccountStatus.CONNECTED.value
    ]

    # 2. Today's session. The one thing that expires every single day.
    if not connected:
        states = ", ".join(sorted({a.status for a in accounts}))
        add(
            "broker session",
            False,
            f"No connected account (status: {states}).",
            "Log in to the broker and set the session token — this is the "
            "daily step; sessions die at midnight IST.",
        )
    else:
        soonest = min(
            (a.session_expires_at for a in connected if a.session_expires_at),
            default=None,
        )
        when = f" Expires {soonest.astimezone(IST):%H:%M IST}." if soonest else ""
        add("broker session", True, f"{len(connected)} connected.{when}")

    # 3. The gates, which are configuration rather than daily state.
    add(
        "live trading gate",
        settings.enable_live_trading,
        "ENABLE_LIVE_TRADING is on." if settings.enable_live_trading
        else "ENABLE_LIVE_TRADING is off; no live order can be placed.",
        None if settings.enable_live_trading
        else "Set ENABLE_LIVE_TRADING=true in the deployment environment.",
    )

    enabled = [a for a in connected if a.live_enabled]
    add(
        "account live-enabled",
        bool(enabled),
        f"{len(enabled)} of {len(connected)} connected account(s) enabled."
        if connected else "No connected account.",
        None if enabled else "Enable live on the account from the Brokers page.",
    )

    # 4. The adapter's own verification state.
    unverified = sorted(
        {
            a.broker
            for a in accounts
            if get_capabilities(Broker(a.broker)).adapter_status != AdapterStatus.WORKING
        }
    )
    add(
        "adapter verified",
        not unverified,
        "All adapters verified." if not unverified
        else f"Not verified: {', '.join(unverified)}.",
        None if not unverified
        else "Place and cancel one real order by hand first, then mark the "
             "adapter verified. Until then every live order is refused.",
    )

    # 5. Kill switch: silent by design, and the easiest thing to leave on.
    engaged = await killswitch.is_global_engaged(get_redis())
    add(
        "kill switch",
        not engaged,
        "Disengaged." if not engaged else "ENGAGED — no strategy will trade.",
        None if not engaged else "Release it on the Risk page.",
    )

    # 6. Something to actually run.
    running = (
        await db.execute(
            select(Strategy).where(
                Strategy.user_id == user_id,
                Strategy.environment == Environment.LIVE.value,
                Strategy.status == StrategyStatus.RUNNING.value,
            )
        )
    ).scalars().all()
    add(
        "running strategies",
        bool(running),
        f"{len(running)} live strategy(ies) running."
        if running else "No live strategy is running.",
        None if running else "Start one on the Strategies page.",
    )

    # 7. Prices. A strategy with no ticks cannot trade even when all else
    # passes: the runner refuses to act without a fresh quote.
    coverage = {"symbols": 0, "resolved": 0, "missing": []}
    for account in connected:
        part = await _instrument_coverage(db, account)
        coverage["symbols"] += part["symbols"]
        coverage["resolved"] += part["resolved"]
        coverage["missing"].extend(part["missing"])
    if coverage["symbols"]:
        add(
            "instrument tokens",
            not coverage["missing"],
            f"{coverage['resolved']} of {coverage['symbols']} symbols resolve."
            if coverage["missing"] else f"All {coverage['symbols']} symbols resolve.",
            None if not coverage["missing"]
            else f"No token for {', '.join(coverage['missing'][:5])} — those "
                 "receive no prices and will not trade. Sync instruments and "
                 "check the symbols are the broker's own codes.",
        )

    return {"ready": all(c["ok"] for c in checks), "checks": checks}
