"""Schemas for the auth surface: enrollment-token minting and JWT refresh."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

_FORBID = ConfigDict(extra="forbid")

# Upper bound on a requested enrollment-token lifetime (30 days). Guards against
# a caller minting an effectively non-expiring token.
_MAX_TOKEN_TTL_SECONDS = 30 * 24 * 3600


class EnrollmentTokenCreateRequest(BaseModel):
    """Body of POST /auth/enrollment-tokens (admin-only)."""

    model_config = _FORBID

    created_by: str = Field(min_length=1, max_length=255)
    # Optional override of the configured default TTL, bounded to a sane range.
    ttl_seconds: int | None = Field(default=None, gt=0, le=_MAX_TOKEN_TTL_SECONDS)


class EnrollmentTokenCreateResponse(BaseModel):
    """The raw token is returned here exactly once and never stored in the clear."""

    id: uuid.UUID
    token: str
    created_at: datetime
    expires_at: datetime


class ChallengeRequest(BaseModel):
    """Body of POST /auth/challenge."""

    model_config = _FORBID

    node_id: uuid.UUID


class ChallengeResponse(BaseModel):
    """A fresh nonce for the agent to sign with its Ed25519 private key."""

    nonce: str
    expires_at: datetime


class TokenRefreshRequest(BaseModel):
    """Body of POST /auth/token/refresh.

    ``signature`` is the base64-encoded Ed25519 signature over the UTF-8 bytes
    of the issued ``nonce`` string.
    """

    model_config = _FORBID

    node_id: uuid.UUID
    nonce: str = Field(min_length=1)
    signature: str = Field(min_length=1)


class TokenResponse(BaseModel):
    """A freshly issued node access token."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int


class EnrollmentTokenOut(BaseModel):
    """Metadata for one minted token in the admin list view.

    Carries no ``token`` and no ``token_hash``: the raw value existed exactly
    once, in the mint response, and the hash is a credential-equivalent lookup
    key that has no business leaving the database.
    """

    id: uuid.UUID
    created_by: str
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None
    revoked_at: datetime | None
    #: Derived server-side from the three timestamps and the clock, so clients
    #: don't each re-implement the precedence and disagree about it.
    status: str


class EnrollmentTokenListResponse(BaseModel):
    """Body of GET /auth/enrollment-tokens."""

    tokens: list[EnrollmentTokenOut]


# --- Human operator auth (ADR-012) -------------------------------------------


class LoginRequest(BaseModel):
    """Body of POST /auth/login."""

    model_config = _FORBID

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class UserOut(BaseModel):
    """A user account as returned to clients. Never includes the hash."""

    id: uuid.UUID
    username: str
    role: str
    created_at: datetime
    last_login_at: datetime | None


class LoginResponse(BaseModel):
    """A freshly issued user access token plus who it belongs to.

    The dashboard renders its admin affordances from ``user.role``; the server
    re-checks the role on every privileged call regardless, so a tampered
    client can reveal a button but not use it.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut
