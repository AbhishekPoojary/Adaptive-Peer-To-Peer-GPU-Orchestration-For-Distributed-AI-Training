"""Admin user management (ADR-012 addendum 2).

- ``GET /users``: every account and why each can or cannot sign in.
- ``POST /users``: create an account (password, Google, or both).
- ``PATCH /users/{id}``: change role, email, password, or enabled state.

Every route requires ADMIN, declared on the *router* rather than per-route so a
route added later is gated by default — the per-route form's failure mode is a
new endpoint silently shipping open, which is how the job surface came to be
unauthenticated before M8.

Why this exists at all
----------------------
ADR-012 made ``scripts/create_user.py`` the way accounts come into being, on the
reasoning that an account is permission to run containers on other people's
machines and so should be granted by someone with shell access to the
orchestrator host. That reasoning holds for the *first* account and no longer
holds for the rest: an ADMIN can already enroll machines and upload datasets
through the API, so "can add a person" is not a larger privilege than what they
demonstrably have. Requiring SSH for it only meant that inviting a classmate
took an admin, a terminal, and a database URL.

There is deliberately still **no self-registration**. An admin creates accounts;
nobody creates their own.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import require_admin_user
from orchestrator.core.db import get_session
from orchestrator.models.user import User, UserRole
from orchestrator.schemas.user import (
    UserAdminOut,
    UserCreateRequest,
    UserListResponse,
    UserUpdateRequest,
)
from orchestrator.services.users import (
    LastAdminError,
    NoCredentialError,
    UserExistsError,
    WeakPasswordError,
    create_user,
    get_user_by_id,
    list_users,
    update_user,
)

logger = logging.getLogger("orchestrator.users")

router = APIRouter(
    prefix="/users", tags=["users"], dependencies=[Depends(require_admin_user)]
)


def _user_out(user: User) -> UserAdminOut:
    return UserAdminOut(
        id=user.id,
        username=user.username,
        role=user.role.value,
        email=user.email,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        disabled_at=user.disabled_at,
        # Derived, never the hash itself.
        has_password=user.password_hash is not None,
        google_linked=user.google_sub is not None,
    )


@router.get("", response_model=UserListResponse)
async def list_users_endpoint(
    session: AsyncSession = Depends(get_session),
) -> UserListResponse:
    """Every account, newest first, including disabled ones."""
    users = await list_users(session)
    return UserListResponse(users=[_user_out(u) for u in users])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=UserAdminOut)
async def create_user_endpoint(
    body: UserCreateRequest,
    admin: User = Depends(require_admin_user),
    session: AsyncSession = Depends(get_session),
) -> UserAdminOut:
    """Create an account.

    A password, an email for Google sign-in, or both. Passing only an email
    creates a Google-only account, which is the shape that makes inviting
    someone a single step: set their address, and their first Google sign-in
    binds to it.
    """
    try:
        user = await create_user(
            session,
            username=body.username,
            password=body.password,
            role=UserRole(body.role),
            email=body.email,
        )
    except UserExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except (WeakPasswordError, NoCredentialError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    await session.commit()
    await session.refresh(user)
    logger.info(
        "user %s created by %s (role=%s, password=%s, email=%s)",
        user.username,
        admin.username,
        user.role.value,
        user.password_hash is not None,
        user.email or "-",
    )
    return _user_out(user)


@router.patch("/{user_id}", response_model=UserAdminOut)
async def update_user_endpoint(
    user_id: uuid.UUID,
    body: UserUpdateRequest,
    admin: User = Depends(require_admin_user),
    session: AsyncSession = Depends(get_session),
) -> UserAdminOut:
    """Change an account's role, email, password, or enabled state.

    Only fields present in the body are touched. ``email: null`` explicitly
    clears the address (and with it Google sign-in), which is different from
    omitting the field.
    """
    user = await get_user_by_id(session, user_id=user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown user"
        )

    fields = body.model_dump(exclude_unset=True)
    try:
        user = await update_user(
            session,
            user=user,
            role=UserRole(body.role) if body.role is not None else None,
            password=body.password,
            email=body.email,
            email_provided="email" in fields,
            disabled=body.disabled,
        )
    except LastAdminError as exc:
        # 409, not 403: the caller has the right to do this in general, and the
        # state of the fleet is what forbids it right now. The message says what
        # to do about it.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except UserExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except (WeakPasswordError, NoCredentialError) as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    await session.commit()
    await session.refresh(user)
    logger.info(
        "user %s updated by %s (%s)",
        user.username,
        admin.username,
        ", ".join(sorted(fields)) or "no fields",
    )
    return _user_out(user)
