"""Node endpoints: enrollment, authenticated self-scoped heartbeat, and the
read-only fleet/detail views the dashboard polls.

Three different auth regimes meet in this one router, which is why the
dependency is declared per-route here rather than router-wide as in
``api/jobs.py``:

* ``POST /nodes/register`` is authenticated by the one-time **enrollment
  token** in the body (ADR-008) — a node has no JWT yet at this point. Rate
  limited, since the token is the thing being guessed.
* ``POST /nodes/{id}/heartbeat`` requires that **node's own** JWT.
* ``GET /nodes`` and ``GET /nodes/{id}`` are dashboard reads and require a
  **user** token (ADR-012). Node telemetry is hardware and utilization data
  about other people's laptops; it is not public.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import (
    assert_node_scope,
    enforce_rate_limit,
    get_node_auth_limiter,
    get_settings_dep,
    require_node_auth,
    require_user,
)
from orchestrator.core.config import Settings
from orchestrator.core.db import get_session
from orchestrator.core.security import PublicKeyError, create_node_jwt
from orchestrator.models.node import Node
from orchestrator.models.user import User, UserRole
from orchestrator.schemas.node import (
    HeartbeatRequest,
    HeartbeatResponse,
    NodeDetailResponse,
    NodeListResponse,
    NodeRegisterRequest,
    NodeRegisterResponse,
)
from orchestrator.services.enrollment import TokenClaimOutcome
from orchestrator.services.nodes import (
    NodeBusyError,
    RegistrationError,
    decommission_node,
    get_node_detail,
    list_nodes,
    record_heartbeat,
    register_node,
)

router = APIRouter(prefix="/nodes", tags=["nodes"])


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=NodeRegisterResponse,
)
async def register(
    request: Request,
    body: NodeRegisterRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> NodeRegisterResponse:
    """Enroll a node: consume the token, persist the node, return a first JWT."""
    enforce_rate_limit(get_node_auth_limiter(), request, bucket="register")
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
        if exc.outcome is TokenClaimOutcome.REVOKED:
            # 403, not 401: the token is genuine and the caller presented it
            # correctly — an admin withdrew it. Saying "invalid" would send an
            # honest operator hunting for a typo.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="enrollment token has been revoked",
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


@router.get(
    "", response_model=NodeListResponse, dependencies=[Depends(require_user)]
)
async def list_nodes_endpoint(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> NodeListResponse:
    """List every enrolled node with its latest telemetry sample."""
    nodes = await list_nodes(session, settings=settings)
    return NodeListResponse(nodes=nodes)


@router.get(
    "/{node_id}", response_model=NodeDetailResponse, dependencies=[Depends(require_user)]
)
async def get_node_endpoint(
    node_id: uuid.UUID,
    samples: int | None = Query(
        default=None,
        ge=1,
        description="Number of recent telemetry samples to include (capped).",
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> NodeDetailResponse:
    """Return one node's current state plus its recent telemetry samples.

    ``samples`` defaults to ``settings.node_detail_default_samples`` and is
    capped at ``settings.node_detail_max_samples`` regardless of what is
    requested.

    # TODO(M8): read-side auth — unauthenticated for dev.
    """
    limit = settings.node_detail_default_samples if samples is None else samples
    limit = min(limit, settings.node_detail_max_samples)

    detail = await get_node_detail(
        session, node_id=node_id, sample_limit=limit, settings=settings
    )
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown node")
    return NodeDetailResponse(
        **detail.summary.model_dump(), telemetry_samples=detail.telemetry_samples
    )


@router.delete("/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_node(
    node_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Remove a machine from the fleet: an admin, or whoever added it.

    The node disappears from the fleet list and its agent can no longer
    authenticate; its history (leases, audits, the jobs it trained) is kept.
    409 while it holds live work. A machine that should come back re-enrolls
    with a new token and joins as a new node.
    """
    if user.role is not UserRole.ADMIN:
        own = await session.get(Node, node_id)
        if own is None or own.decommissioned_at is not None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown node")
        if own.enrolled_by != user.username:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="you can only remove computers you added",
            )
    try:
        node = await decommission_node(session, node_id=node_id)
    except NodeBusyError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this node is running a job; cancel it or wait for it to finish",
        ) from exc
    if node is None:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown node")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
