"""Enrollment-token service: minting and race-safe single-use consumption.

The single-use guarantee is enforced by a conditional atomic UPDATE
(``... WHERE used_at IS NULL AND expires_at > now() RETURNING id``). Under
Postgres READ COMMITTED, two concurrent claims of the same token serialize on
the row lock: the first to commit sets ``used_at``; the second re-evaluates the
predicate against the committed row, matches zero rows, and loses. Exactly one
winner, with no application-level locking.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.security import generate_opaque_token, hash_token
from orchestrator.models.enrollment import EnrollmentToken


class TokenClaimOutcome(enum.Enum):
    """Why a token claim succeeded or failed — maps to distinct HTTP codes."""

    CLAIMED = "claimed"
    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    ALREADY_USED = "already_used"


async def create_enrollment_token(
    session: AsyncSession, *, created_by: str, ttl_seconds: int
) -> tuple[EnrollmentToken, str]:
    """Mint a new enrollment token. Returns the ORM row and the raw token.

    Only the SHA-256 hash of the raw token is stored; the caller is responsible
    for returning the raw value to the operator exactly once. Flushes so the
    server-defaulted ``created_at`` is populated; the caller commits.
    """
    raw_token = generate_opaque_token()
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    token = EnrollmentToken(
        token_hash=hash_token(raw_token),
        created_by=created_by,
        expires_at=expires_at,
    )
    session.add(token)
    await session.flush()
    await session.refresh(token)
    return token, raw_token


async def claim_enrollment_token(
    session: AsyncSession, *, raw_token: str
) -> tuple[TokenClaimOutcome, uuid.UUID | None]:
    """Atomically consume a token. Does not commit — the caller owns the txn.

    Returns ``(CLAIMED, token_id)`` on success. On failure, classifies why by
    reading the (now committed) row so the API can distinguish an unknown token
    (401) from one already used by a racing enrollment (409).
    """
    token_hash = hash_token(raw_token)
    stmt = (
        update(EnrollmentToken)
        .where(
            EnrollmentToken.token_hash == token_hash,
            EnrollmentToken.used_at.is_(None),
            EnrollmentToken.expires_at > func.now(),
        )
        .values(used_at=func.now())
        .returning(EnrollmentToken.id)
    )
    result = await session.execute(stmt)
    claimed_id = result.scalar_one_or_none()
    if claimed_id is not None:
        return TokenClaimOutcome.CLAIMED, claimed_id

    existing = (
        await session.execute(
            select(EnrollmentToken).where(EnrollmentToken.token_hash == token_hash)
        )
    ).scalar_one_or_none()
    if existing is None:
        return TokenClaimOutcome.NOT_FOUND, None
    if existing.used_at is not None:
        return TokenClaimOutcome.ALREADY_USED, None
    return TokenClaimOutcome.EXPIRED, None
