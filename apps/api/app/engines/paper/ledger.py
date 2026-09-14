"""Append-only cash movements. Writers hold the account lock until commit."""

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select

from app.db.models import BrokerAccount, CashLedger, FundsSnapshot

INITIAL_PAPER_CASH = Decimal("1000000.00")


def money(value):
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def lock_account(db, account_id):
    # PostgreSQL FOR NO KEY UPDATE serializes writers while remaining compatible
    # with the KEY SHARE locks taken by concurrent order foreign-key inserts.
    return (
        await db.execute(
            select(BrokerAccount)
            .where(BrokerAccount.id == account_id)
            .with_for_update(key_share=True)
        )
    ).scalar_one()


async def latest(db, account_id):
    return (
        await db.execute(
            select(CashLedger)
            .where(CashLedger.broker_account_id == account_id)
            .order_by(CashLedger.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_cash(db, account_id):
    entry = await latest(db, account_id)
    if entry is not None:
        return entry.balance
    snapshot = (
        await db.execute(
            select(FundsSnapshot)
            .where(FundsSnapshot.broker_account_id == account_id)
            .order_by(FundsSnapshot.ts.desc(), FundsSnapshot.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return snapshot.available_cash if snapshot else INITIAL_PAPER_CASH


async def append(db, account_id, entry_type, amount, *, fill_id=None):
    # Reentrant within one transaction; also protects first-entry creation.
    await lock_account(db, account_id)
    entry = await latest(db, account_id)
    if entry is None:
        balance = await get_cash(db, account_id)
        db.add(
            CashLedger(
                broker_account_id=account_id, entry_type="OPENING", amount=balance, balance=balance
            )
        )
        await db.flush()
    else:
        balance = entry.balance
    amount = money(amount)
    entry = CashLedger(
        broker_account_id=account_id,
        entry_type=entry_type,
        amount=amount,
        balance=balance + amount,
        fill_id=fill_id,
    )
    db.add(entry)
    await db.flush()
    return entry.balance


async def reset(db, account_id):
    await lock_account(db, account_id)
    cash = await get_cash(db, account_id)
    await append(db, account_id, "RESET", -cash)
    await append(db, account_id, "OPENING", INITIAL_PAPER_CASH)
