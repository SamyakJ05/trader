"""Create or promote the first operator account.

Registration is invite-only and invites require an inviter, so a fresh
deployment has no way to create its first admin through the API. This is that
bootstrap, run from the host:

    python -m app.seeds.bootstrap_admin you@example.com

If the account exists it is promoted; otherwise it is created and a password is
generated and printed once. Printing beats prompting so the command works
non-interactively (docker compose exec, CI), and the operator changes it after
first sign-in.
"""

import asyncio
import secrets
import sys

from sqlalchemy import select

from app.core.security import hash_password
from app.db.models import User
from app.db.session import async_session_factory


async def bootstrap(email: str) -> None:
    email = email.strip().lower()
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        if user is None:
            password = secrets.token_urlsafe(12)
            user = User(
                email=email,
                password_hash=hash_password(password),
                full_name=None,
                is_admin=True,
                is_active=True,
            )
            db.add(user)
            await db.commit()
            print(f"created operator {email}")
            print(f"temporary password: {password}")
            print("Sign in and change it, then invite other users from /admin.")
            return

        changed = []
        if not user.is_admin:
            user.is_admin = True
            changed.append("promoted to operator")
        if not user.is_active:
            user.is_active = True
            changed.append("reactivated")
        await db.commit()
        print(f"{email}: {', '.join(changed) if changed else 'already an active operator'}")


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m app.seeds.bootstrap_admin <email>", file=sys.stderr)
        raise SystemExit(2)
    asyncio.run(bootstrap(sys.argv[1]))


if __name__ == "__main__":
    main()
