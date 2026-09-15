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
