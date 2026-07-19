"""Shared FastAPI dependencies for auth and request-scoped resources.

``require_node_auth`` validates a node's bearer JWT and yields the live Node.
``assert_node_scope`` is the per-node authorization primitive that later
endpoints (Sonnet's read APIs) reuse to enforce that a token only acts on its
own node.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.config import Settings, get_settings
from orchestrator.core.db import get_session
from orchestrator.core.security import (
    JWTValidationError,
    constant_time_equals,
    decode_node_jwt,
)
from orchestrator.models.node import Node

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
