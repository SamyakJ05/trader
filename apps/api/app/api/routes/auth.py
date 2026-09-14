import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.core.config import get_settings
from app.core.deps import CurrentUser, DbSession
from app.core.security import hash_password, new_session_token, verify_password
from app.db.models import AuthSession, User
from app.domain.enums import AuditEventType
from app.services import audit


EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_user_email(email: str) -> str:
    if not EMAIL_REGEX.match(email):
        raise ValueError("value is not a valid email address")
    return email.lower()

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return validate_user_email(value)


class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return validate_user_email(value)


class UserOut(BaseModel):
    id: str
    email: str
    full_name: str | None
    is_admin: bool = False


class AuthResponse(BaseModel):
    token: str
    expires_at: datetime
    user: UserOut


async def _create_session(db: DbSession, user: User) -> AuthResponse:
    token = new_session_token()
    expires = datetime.now(timezone.utc) + timedelta(hours=get_settings().session_ttl_hours)
    db.add(AuthSession(user_id=user.id, token=token, expires_at=expires))
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=user.id,
        entity_type="session",
        payload={"action": "login"},
    )
    await db.commit()
    return AuthResponse(
        token=token,
        expires_at=expires,
        user=UserOut(
            id=str(user.id),
            email=user.email,
            full_name=user.full_name,
            is_admin=user.is_admin,
        ),
    )


@router.post("/register", response_model=AuthResponse, status_code=201)
async def register(body: RegisterRequest, db: DbSession):
    existing = await db.execute(select(User).where(User.email == body.email.lower()))
    if existing.scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    user = User(
        email=body.email.lower(),
        password_hash=hash_password(body.password),
        full_name=body.full_name,
    )
    db.add(user)
    await db.flush()
    return await _create_session(db, user)


@router.post("/login", response_model=AuthResponse)
async def login(body: LoginRequest, db: DbSession):
    result = await db.execute(select(User).where(User.email == body.email.lower()))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    return await _create_session(db, user)


@router.post("/logout", status_code=204)
async def logout(user: CurrentUser, db: DbSession):
    from sqlalchemy import delete

    await db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
    await audit.emit(
        db,
        AuditEventType.USER_ACTION,
        user_id=user.id,
        entity_type="session",
        payload={"action": "logout"},
    )
    await db.commit()


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser):
    return UserOut(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        is_admin=user.is_admin,
    )
