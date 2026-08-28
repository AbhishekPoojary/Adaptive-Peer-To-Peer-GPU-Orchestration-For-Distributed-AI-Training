"""Schemas for admin user management (ADR-012 addendum 2).

Distinct from the ``UserOut`` in ``schemas/auth.py``, which is what a person sees
about *themselves* after signing in. These are the administrative views: they
carry the account's state (disabled, how it can sign in) that an admin needs in
order to answer "why can't this person get in?" without shell access.

Nothing here ever carries ``password_hash``. It is not merely omitted from the
response model — it is never assembled into one of these objects at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orchestrator.services.users import MIN_PASSWORD_LENGTH

_FORBID = ConfigDict(extra="forbid")

#: Usernames are the login handle and appear in job attribution, so they are
#: constrained to something unambiguous: letters, digits, dot, dash, underscore.
USERNAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$"


class UserAdminOut(BaseModel):
    """One account as an admin sees it."""

    id: uuid.UUID
    username: str
    role: str
    email: str | None
    created_at: datetime
    last_login_at: datetime | None
    #: Set means the account cannot sign in by any method.
    disabled_at: datetime | None
    #: Whether a password is set. Derived from the hash's presence — the hash
    #: itself never leaves the database. A Google-only account has no password,
    #: and an admin needs to know that to answer "why can't they log in?".
    has_password: bool
    #: Whether a Google identity has been bound yet. False with an email set
    #: means "invited, has not signed in yet", which is a different situation
    #: from "not invited" and is worth being able to tell apart.
    google_linked: bool


class UserListResponse(BaseModel):
    """Body of GET /users."""

    users: list[UserAdminOut]


class UserCreateRequest(BaseModel):
    """Body of POST /users (admin-only).

    Exactly mirrors what ``scripts/create_user.py`` can do, so the API and the
    bootstrap script cannot drift into disagreeing about what an account is.
    """

    model_config = _FORBID

    username: str = Field(pattern=USERNAME_PATTERN)
    role: str = Field(pattern=r"^(ADMIN|OPERATOR)$")
    #: Omit for a Google-only account, in which case ``email`` is required.
    password: str | None = Field(default=None, min_length=1, max_length=1024)
    #: The address Google sign-in matches. Required when there is no password,
    #: because the account would otherwise have no way in at all.
    email: str | None = Field(default=None, max_length=320)

    @model_validator(mode="after")
    def _needs_some_way_in(self) -> UserCreateRequest:
        if self.password is None and self.email is None:
            raise ValueError(
                "an account needs a password, an email for Google sign-in, or both"
            )
        if self.password is not None and len(self.password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"password must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        return self


class UserUpdateRequest(BaseModel):
    """Body of PATCH /users/{id} (admin-only).

    Every field is optional; only what is present is changed. ``None`` is a
    meaningful value for ``email`` — it *unsets* the address and with it Google
    sign-in — so absence and null have to mean different things. That is why this
    is a PATCH with explicit presence checks rather than a PUT.
    """

    model_config = _FORBID

    role: str | None = Field(default=None, pattern=r"^(ADMIN|OPERATOR)$")
    password: str | None = Field(default=None, min_length=1, max_length=1024)
    email: str | None = Field(default=None, max_length=320)
    #: True disables the account, False re-enables it.
    disabled: bool | None = None

    @model_validator(mode="after")
    def _password_meets_the_floor(self) -> UserUpdateRequest:
        if self.password is not None and len(self.password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"password must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        return self
