"""Node endpoints: enrollment and authenticated, self-scoped heartbeat.

Read endpoints for nodes/telemetry are a later (Sonnet) task; this module owns
only the two write paths the identity/telemetry foundation needs.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import assert_node_scope, get_settings_dep, require_node_auth
from orchestrator.core.config import Settings
from orchestrator.core.db import get_session
from orchestrator.core.security import PublicKeyError, create_node_jwt
from orchestrator.models.node import Node
from orchestrator.schemas.node import (
    HeartbeatRequest,
    HeartbeatResponse,
    NodeRegisterRequest,
    NodeRegisterResponse,
)
from orchestrator.services.enrollment import TokenClaimOutcome
from orchestrator.services.nodes import RegistrationError, record_heartbeat, register_node

router = APIRouter(prefix="/nodes", tags=["nodes"])


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=NodeRegisterResponse,
)
async def register(
    body: NodeRegisterRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> NodeRegisterResponse:
    """Enroll a node: consume the token, persist the node, return a first JWT."""
    try:
        node = await register_node(session, req=body, settings=settings)
    except PublicKeyError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="public_key is not a valid Ed25519 PEM key",
        ) from exc
    except RegistrationError as exc:
        await session.rollback()
        if exc.outcome is TokenClaimOutcome.ALREADY_USED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="enrollment token already used",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired enrollment token",
        ) from exc

    await session.commit()

    access_token = create_node_jwt(
        node_id=str(node.id),
        signing_key=settings.jwt_signing_key,
        ttl_seconds=settings.jwt_access_token_ttl_seconds,
    )
    return NodeRegisterResponse(
        node_id=node.id,
        name=node.name,
        access_token=access_token,
        expires_in=settings.jwt_access_token_ttl_seconds,
    )


@router.post("/{node_id}/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    node_id: uuid.UUID,
    body: HeartbeatRequest,
    node: Node = Depends(require_node_auth),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> HeartbeatResponse:
    """Record a heartbeat for the authenticated, self-scoped node."""
    assert_node_scope(node_id, node)
    result = await record_heartbeat(
        session, node=node, payload=body, settings=settings
    )
    await session.commit()
    return HeartbeatResponse(
        server_time=result.server_time,
        last_heartbeat_at=result.last_heartbeat_at,
        status=node.status.value,
        rtt_ewma_ms=result.rtt_ewma_ms,
    )
