"""Auth endpoints (ADR-008): enrollment-token minting and JWT refresh.

- ``POST /auth/enrollment-tokens`` (admin): mint a single-use token.
- ``POST /auth/challenge``: issue a nonce for challenge-response.
- ``POST /auth/token/refresh``: prove key possession by signing the nonce,
  receive a fresh JWT.
"""

from __future__ import annotations

import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import get_settings_dep, require_admin
from orchestrator.core.config import Settings
from orchestrator.core.db import get_session
from orchestrator.core.security import create_node_jwt, verify_ed25519_signature
from orchestrator.models.node import Node
from orchestrator.schemas.auth import (
    ChallengeRequest,
    ChallengeResponse,
    EnrollmentTokenCreateRequest,
    EnrollmentTokenCreateResponse,
    TokenRefreshRequest,
    TokenResponse,
)
from orchestrator.services.challenge import consume_challenge, create_challenge
from orchestrator.services.enrollment import create_enrollment_token

router = APIRouter(prefix="/auth", tags=["auth"])

_INVALID_CHALLENGE = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or expired challenge"
)


@router.post(
    "/enrollment-tokens",
    status_code=status.HTTP_201_CREATED,
    response_model=EnrollmentTokenCreateResponse,
    dependencies=[Depends(require_admin)],
)
async def create_enrollment_token_endpoint(
    body: EnrollmentTokenCreateRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> EnrollmentTokenCreateResponse:
    """Mint a single-use enrollment token. Returns the raw token exactly once."""
    ttl_seconds = body.ttl_seconds or settings.enrollment_token_ttl_seconds
    token, raw_token = await create_enrollment_token(
        session, created_by=body.created_by, ttl_seconds=ttl_seconds
    )
    await session.commit()
    return EnrollmentTokenCreateResponse(
        id=token.id,
        token=raw_token,
        created_at=token.created_at,
        expires_at=token.expires_at,
    )


@router.post("/challenge", response_model=ChallengeResponse)
async def issue_challenge(
    body: ChallengeRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> ChallengeResponse:
    """Issue a single-use nonce for the given node to sign."""
    node = await session.get(Node, body.node_id)
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown node"
        )
    challenge = await create_challenge(
        session, node_id=body.node_id, ttl_seconds=settings.auth_nonce_ttl_seconds
    )
    await session.commit()
    return ChallengeResponse(nonce=challenge.nonce, expires_at=challenge.expires_at)


@router.post("/token/refresh", response_model=TokenResponse)
async def refresh_token(
    body: TokenRefreshRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> TokenResponse:
    """Verify a signed nonce and mint a new JWT.

    The nonce is consumed atomically first (single-use); then the Ed25519
    signature over the nonce bytes is verified against the node's registered
    public key. Any failure is a flat 401.
    """
    consumed = await consume_challenge(session, node_id=body.node_id, nonce=body.nonce)
    if not consumed:
        # Unknown / expired / already-used / wrong-node nonce.
        await session.rollback()
        raise _INVALID_CHALLENGE

    node = await session.get(Node, body.node_id)
    if node is None:  # node deleted between challenge and refresh
        await session.commit()
        raise _INVALID_CHALLENGE

    try:
        signature = base64.b64decode(body.signature, validate=True)
    except (binascii.Error, ValueError) as exc:
        await session.commit()  # nonce is single-use: it stays consumed
        raise _INVALID_CHALLENGE from exc

    if not verify_ed25519_signature(
        pem=node.public_key, message=body.nonce.encode("utf-8"), signature=signature
    ):
        await session.commit()  # nonce stays consumed even on bad signature
        raise _INVALID_CHALLENGE

    access_token = create_node_jwt(
        node_id=str(node.id),
        signing_key=settings.jwt_signing_key,
        ttl_seconds=settings.jwt_access_token_ttl_seconds,
    )
    await session.commit()
    return TokenResponse(
        access_token=access_token, expires_in=settings.jwt_access_token_ttl_seconds
    )
