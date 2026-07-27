"""Shared FastAPI dependencies for auth and request-scoped resources.

Two independent identity layers meet here and must never be confused:

* **Nodes** (ADR-008) authenticate with an Ed25519 signature and carry a JWT
  with ``aud="node"``. ``require_node_auth`` yields the live Node;
  ``assert_node_scope`` enforces that a node's token only acts on itself.
* **Users** (ADR-012) authenticate with a password and carry a JWT with
  ``aud="user"``. ``require_user`` yields the live User; ``require_admin_user``
  additionally demands the ADMIN role.

Both are signed with the same key, so the audience check in the decoders is
the entire boundary between a peer machine and a human operator. It is
verified in both directions and has explicit negative tests.
"""

from __future__ import annotations

import uuid
from functools import lru_cache

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.config import Settings, get_settings
from orchestrator.core.db import get_session
from orchestrator.core.security import (
    JWTValidationError,
    constant_time_equals,
    decode_node_jwt,
    decode_user_jwt,
)
from orchestrator.models.node import Node
from orchestrator.models.user import User, UserRole
from orchestrator.services.ratelimit import FixedWindowRateLimiter

_BEARER_PREFIX = "Bearer "
_UNAUTH_HEADERS = {"WWW-Authenticate": "Bearer"}


def get_settings_dep() -> Settings:
    """FastAPI-injectable accessor for the process-wide settings."""
    return get_settings()


def require_admin(
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    settings: Settings = Depends(get_settings_dep),
) -> None:
    """Gate the admin bootstrap surface behind the static ``ADMIN_API_KEY``.

    503 if the server has no admin key configured (the surface is disabled),
    401 on a missing or non-matching key (compared in constant time).
    """
    if settings.admin_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="admin API is not configured",
        )
    if x_admin_key is None or not constant_time_equals(x_admin_key, settings.admin_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin key"
        )


async def require_node_auth(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> Node:
    """Validate the bearer JWT and return the authenticated Node.

    Rejects (401) a missing/malformed header, and any token that fails
    signature, expiry, or audience checks, or whose node no longer exists.
    """
    if authorization is None or not authorization.startswith(_BEARER_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers=_UNAUTH_HEADERS,
        )
    token = authorization[len(_BEARER_PREFIX) :].strip()
    try:
        subject = decode_node_jwt(token, signing_key=settings.jwt_signing_key)
        node_id = uuid.UUID(subject)
    except (JWTValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
            headers=_UNAUTH_HEADERS,
        ) from exc

    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unknown node",
            headers=_UNAUTH_HEADERS,
        )
    return node


def assert_node_scope(path_node_id: uuid.UUID, node: Node) -> None:
    """Assert the authenticated node matches the path node id, else 403.

    The per-node scoping primitive reused by every self-scoped endpoint.
    """
    if node.id != path_node_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="token is not scoped to this node",
        )


# --- Human operator auth (ADR-012) -------------------------------------------


def _bearer_token(authorization: str | None) -> str:
    """Extract the bearer token, or raise the shared 401."""
    if authorization is None or not authorization.startswith(_BEARER_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers=_UNAUTH_HEADERS,
        )
    return authorization[len(_BEARER_PREFIX) :].strip()


async def require_user(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> User:
    """Validate a human operator's bearer JWT and return the live User.

    A node token is rejected here despite being validly signed, because
    ``decode_user_jwt`` requires ``aud="user"``. The user row is re-read on
    every request rather than trusted from the token's claims, so disabling an
    account takes effect immediately instead of at the next token expiry.
    """
    token = _bearer_token(authorization)
    try:
        subject, _username, _role = decode_user_jwt(
            token, signing_key=settings.jwt_signing_key
        )
        user_id = uuid.UUID(subject)
    except (JWTValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
            headers=_UNAUTH_HEADERS,
        ) from exc

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unknown or disabled user",
            headers=_UNAUTH_HEADERS,
        )
    return user


async def require_admin_user(user: User = Depends(require_user)) -> User:
    """Require an authenticated user holding the ADMIN role, else 403.

    The role comes from the freshly-read row, not the token claim, so a
    demotion is effective at once.
    """
    if user.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this action requires the ADMIN role",
        )
    return user


async def require_admin_key_or_admin_user(
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> None:
    """Accept *either* the static ADMIN_API_KEY *or* an ADMIN user token.

    The static key survives for exactly one purpose: out-of-band CLI bootstrap
    before any user account exists (ADR-012 §6). Interactive clients use a user
    token — the dashboard no longer holds the admin key at all.
    """
    if x_admin_key is not None:
        if settings.admin_api_key is not None and constant_time_equals(
            x_admin_key, settings.admin_api_key
        ):
            return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin key"
        )
    user = await require_user(
        authorization=authorization, session=session, settings=settings
    )
    await require_admin_user(user=user)


# --- Rate limiting (ADR-012 §7) ----------------------------------------------


@lru_cache(maxsize=1)
def get_login_limiter() -> FixedWindowRateLimiter:
    """Process-wide limiter for password login attempts."""
    settings = get_settings()
    return FixedWindowRateLimiter(
        limit=settings.login_rate_limit_attempts,
        window_seconds=settings.login_rate_limit_window_seconds,
    )


@lru_cache(maxsize=1)
def get_node_auth_limiter() -> FixedWindowRateLimiter:
    """Process-wide limiter for node challenge/refresh/register."""
    settings = get_settings()
    return FixedWindowRateLimiter(
        limit=settings.node_auth_rate_limit_attempts,
        window_seconds=settings.node_auth_rate_limit_window_seconds,
    )


def reset_rate_limiters() -> None:
    """Drop cached limiters and their windows. For test isolation only."""
    get_login_limiter.cache_clear()
    get_node_auth_limiter.cache_clear()


def client_key(request: Request) -> str:
    """Rate-limit bucket key for a request: the peer IP, or "unknown".

    Deliberately does **not** trust ``X-Forwarded-For``. Behind an untrusted
    network any client could set that header and mint itself unlimited buckets,
    which would turn the limiter off while appearing to work. ADR-010 puts the
    orchestrator on the overlay with no reverse proxy in front, so the socket
    peer *is* the client. If a trusted proxy is ever added, this must be taught
    which hop to trust — not simply switched to the header.
    """
    return request.client.host if request.client is not None else "unknown"


def enforce_rate_limit(
    limiter: FixedWindowRateLimiter, request: Request, *, bucket: str
) -> None:
    """Raise 429 with ``Retry-After`` if this client is over the limit."""
    decision = limiter.check(f"{bucket}:{client_key(request)}")
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many attempts; slow down",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )
