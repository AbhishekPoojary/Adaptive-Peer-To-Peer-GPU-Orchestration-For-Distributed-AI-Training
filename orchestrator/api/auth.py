"""Auth endpoints (ADR-008 machine identity, ADR-012 human identity).

Node (machine) identity:
- ``POST /auth/challenge``: issue a nonce for challenge-response.
- ``POST /auth/token/refresh``: prove key possession by signing the nonce,
  receive a fresh ``aud="node"`` JWT.

Human identity:
- ``POST /auth/login``: password -> short-lived ``aud="user"`` JWT.
- ``GET /auth/me``: who the presented user token belongs to.

Fleet administration (static admin key **or** an ADMIN user token):
- ``POST /auth/enrollment-tokens``: mint a single-use token.
- ``GET /auth/enrollment-tokens``: list minted tokens (metadata only).
- ``POST /auth/enrollment-tokens/{id}/revoke``: withdraw an unused token.

Every endpoint that accepts a guessable secret is rate limited per client IP
(ADR-012 §7).
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import (
    enforce_rate_limit,
    get_login_limiter,
    get_node_auth_limiter,
    get_settings_dep,
    require_admin_key_or_admin_user,
    require_user,
)
from orchestrator.core.config import Settings
from orchestrator.core.db import get_session
from orchestrator.core.security import (
    create_node_jwt,
    create_user_jwt,
    verify_ed25519_signature,
)
from orchestrator.models.enrollment import EnrollmentToken
from orchestrator.models.node import Node
from orchestrator.models.user import User
from orchestrator.schemas.auth import (
    ChallengeRequest,
    ChallengeResponse,
    EnrollmentTokenCreateRequest,
    EnrollmentTokenCreateResponse,
    EnrollmentTokenListResponse,
    EnrollmentTokenOut,
    LoginRequest,
    LoginResponse,
    TokenRefreshRequest,
    TokenResponse,
    UserOut,
)
from orchestrator.services.challenge import consume_challenge, create_challenge
from orchestrator.services.enrollment import (
    TokenRevokeOutcome,
    create_enrollment_token,
    list_enrollment_tokens,
    revoke_enrollment_token,
)
from orchestrator.services.users import authenticate_user, record_login

router = APIRouter(prefix="/auth", tags=["auth"])

_INVALID_CHALLENGE = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or expired challenge"
)

# One message for every login failure. Distinguishing "no such user" from
# "wrong password" hands an attacker a free username oracle; services.users
# also equalizes the timing so the response itself doesn't leak it either.
_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="invalid username or password",
    headers={"WWW-Authenticate": "Bearer"},
)


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        role=user.role.value,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


def _token_status(token: EnrollmentToken, *, now: datetime) -> str:
    """Classify a token's real state. Precedence is what actually happened
    first: a used token stays "used" even once its window has passed."""
    if token.used_at is not None:
        return "used"
    if token.revoked_at is not None:
        return "revoked"
    if token.expires_at <= now:
        return "expired"
    return "active"


# --- Fleet administration ----------------------------------------------------


@router.post(
    "/enrollment-tokens",
    status_code=status.HTTP_201_CREATED,
    response_model=EnrollmentTokenCreateResponse,
    dependencies=[Depends(require_admin_key_or_admin_user)],
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


@router.get(
    "/enrollment-tokens",
    response_model=EnrollmentTokenListResponse,
    dependencies=[Depends(require_admin_key_or_admin_user)],
)
async def list_enrollment_tokens_endpoint(
    session: AsyncSession = Depends(get_session),
) -> EnrollmentTokenListResponse:
    """List minted tokens, newest first. Metadata only — never the secret."""
    now = datetime.now(UTC)
    tokens = await list_enrollment_tokens(session)
    return EnrollmentTokenListResponse(
        tokens=[
            EnrollmentTokenOut(
                id=t.id,
                created_by=t.created_by,
                created_at=t.created_at,
                expires_at=t.expires_at,
                used_at=t.used_at,
                revoked_at=t.revoked_at,
                status=_token_status(t, now=now),
            )
            for t in tokens
        ]
    )


@router.post(
    "/enrollment-tokens/{token_id}/revoke",
    response_model=EnrollmentTokenOut,
    dependencies=[Depends(require_admin_key_or_admin_user)],
)
async def revoke_enrollment_token_endpoint(
    token_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> EnrollmentTokenOut:
    """Withdraw an unused token so it can never enroll a node.

    Returns the token's resulting state rather than an empty 204, so the
    caller sees the committed truth (including the revocation timestamp)
    instead of inferring it.
    """
    outcome = await revoke_enrollment_token(session, token_id=token_id)
    if outcome is TokenRevokeOutcome.NOT_FOUND:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown enrollment token"
        )
    if outcome is TokenRevokeOutcome.ALREADY_USED:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "token was already used to enroll a node; revoking it now would "
                "not remove that node's access"
            ),
        )
    # ALREADY_REVOKED is not an error: the caller's intent already holds, and
    # reporting a conflict would make a retry after a dropped response look
    # like a failure.
    await session.commit()

    token = await session.get(EnrollmentToken, token_id)
    assert token is not None  # just revoked and committed
    return EnrollmentTokenOut(
        id=token.id,
        created_by=token.created_by,
        created_at=token.created_at,
        expires_at=token.expires_at,
        used_at=token.used_at,
        revoked_at=token.revoked_at,
        status=_token_status(token, now=datetime.now(UTC)),
    )


# --- Human operator auth (ADR-012) -------------------------------------------


@router.post("/login", response_model=LoginResponse)
async def login(
    request: Request,
    body: LoginRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> LoginResponse:
    """Exchange a username and password for a short-lived user access token."""
    enforce_rate_limit(get_login_limiter(), request, bucket="login")

    user = await authenticate_user(
        session, username=body.username, password=body.password
    )
    if user is None:
        raise _INVALID_CREDENTIALS

    await record_login(session, user_id=user.id)
    await session.commit()
    await session.refresh(user)

    access_token = create_user_jwt(
        user_id=str(user.id),
        username=user.username,
        role=user.role.value,
        signing_key=settings.jwt_signing_key,
        ttl_seconds=settings.user_access_token_ttl_seconds,
    )
    return LoginResponse(
        access_token=access_token,
        expires_in=settings.user_access_token_ttl_seconds,
        user=_user_out(user),
    )


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(require_user)) -> UserOut:
    """Return the account the presented user token belongs to.

    The dashboard calls this on load to decide whether a stored token is still
    good, rather than trusting a locally cached copy of the user.
    """
    return _user_out(user)


# --- Node (machine) auth (ADR-008) -------------------------------------------


@router.post("/challenge", response_model=ChallengeResponse)
async def issue_challenge(
    request: Request,
    body: ChallengeRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> ChallengeResponse:
    """Issue a single-use nonce for the given node to sign."""
    enforce_rate_limit(get_node_auth_limiter(), request, bucket="challenge")

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
    request: Request,
    body: TokenRefreshRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> TokenResponse:
    """Verify a signed nonce and mint a new JWT.

    The nonce is consumed atomically first (single-use); then the Ed25519
    signature over the nonce bytes is verified against the node's registered
    public key. Any failure is a flat 401.
    """
    enforce_rate_limit(get_node_auth_limiter(), request, bucket="token-refresh")

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
