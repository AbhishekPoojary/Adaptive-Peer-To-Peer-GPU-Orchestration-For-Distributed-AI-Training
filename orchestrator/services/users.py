"""User account service (ADR-012): creation and password authentication.

Authentication deliberately does the same amount of work whether or not the
username exists — see :func:`authenticate_user`.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.security import hash_password, verify_password
from orchestrator.models.user import User, UserRole

#: A syntactically valid scrypt hash of a random throwaway password, computed
#: once at import. authenticate_user verifies the submitted password against
#: this when the username does not exist, so a miss costs the same ~50-100 ms
#: as a hit. Without it, "username not found" returns fast enough to enumerate
#: valid usernames by timing alone.
_DUMMY_HASH = hash_password(uuid.uuid4().hex)

#: Minimum password length accepted at creation. Not a strength oracle — just
#: a floor that stops a one-character password from reaching the database.
MIN_PASSWORD_LENGTH = 12


class UserExistsError(Exception):
    """Raised when the requested username is already taken."""


class WeakPasswordError(Exception):
    """Raised when a password fails the length floor."""


async def create_user(
    session: AsyncSession, *, username: str, password: str, role: UserRole
) -> User:
    """Create an account with a scrypt-hashed password. Caller commits.

    Uniqueness is enforced by the database's unique index and the resulting
    IntegrityError is translated here, rather than by a check-then-insert that
    two concurrent creations could both pass.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    user = User(
        username=username,
        password_hash=hash_password(password),
        role=role,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise UserExistsError(f"username {username!r} is already taken") from exc
    await session.refresh(user)
    return user


async def get_user_by_username(session: AsyncSession, *, username: str) -> User | None:
    """Look up an account by its unique username."""
    result = await session.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def authenticate_user(
    session: AsyncSession, *, username: str, password: str
) -> User | None:
    """Return the user iff the password matches and the account is enabled.

    Runs the KDF against a dummy hash when the username is unknown so the
    response time does not distinguish "no such user" from "wrong password".
    The caller must also return an identical error message for both.
    """
    user = await get_user_by_username(session, username=username)
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.password_hash):
        return None
    if not user.is_active:
        # Verified the password first, deliberately: branching on the disabled
        # flag before doing the KDF work would reintroduce the timing signal.
        return None
    return user


async def record_login(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    """Stamp ``last_login_at`` with the server clock. Caller commits."""
    await session.execute(
        update(User).where(User.id == user_id).values(last_login_at=func.now())
    )
