"""Challenge-nonce service: issue and race-safe single-use consumption.

Same atomic-UPDATE discipline as enrollment tokens: a nonce is consumed with
``... WHERE nonce = :n AND node_id = :id AND used_at IS NULL AND expires_at >
now() RETURNING id``. Binding ``node_id`` in the predicate means a nonce issued
for one node cannot be replayed to refresh another node's token.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.security import generate_opaque_token
from orchestrator.models.nonce import AuthChallenge


async def create_challenge(
    session: AsyncSession, *, node_id: uuid.UUID, ttl_seconds: int
) -> AuthChallenge:
    """Issue a fresh single-use nonce bound to ``node_id``. Caller commits."""
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    challenge = AuthChallenge(
        node_id=node_id,
        nonce=generate_opaque_token(),
        expires_at=expires_at,
    )
    session.add(challenge)
    await session.flush()
    await session.refresh(challenge)
    return challenge


async def consume_challenge(
    session: AsyncSession, *, node_id: uuid.UUID, nonce: str
) -> bool:
    """Atomically consume a nonce for ``node_id``. Does not commit.

    Returns True iff a live, unused nonce matched and was marked used. Returns
    False for unknown, expired, already-used, or wrong-node nonces — all of
    which the API surfaces as 401.
    """
    stmt = (
        update(AuthChallenge)
        .where(
            AuthChallenge.nonce == nonce,
            AuthChallenge.node_id == node_id,
            AuthChallenge.used_at.is_(None),
            AuthChallenge.expires_at > func.now(),
        )
        .values(used_at=func.now())
        .returning(AuthChallenge.id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None
