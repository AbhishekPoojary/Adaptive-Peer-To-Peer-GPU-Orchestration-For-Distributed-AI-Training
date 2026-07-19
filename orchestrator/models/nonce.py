"""Challenge nonce model (ADR-008, stage 3 — challenge/response refresh).

``POST /auth/challenge`` issues a random nonce bound to a node; the agent signs
it with its Ed25519 private key and presents the signature to
``POST /auth/token/refresh``. Nonces are single-use (consumed atomically) and
expire quickly, so a replayed or stale challenge cannot mint a new JWT.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from orchestrator.core.db import Base


class AuthChallenge(Base):
    """A single-use, expiring nonce for challenge-response JWT refresh."""

    __tablename__ = "auth_challenges"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Random URL-safe nonce string. The agent signs its UTF-8 bytes verbatim.
    nonce: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set exactly once, atomically, when the nonce is consumed. NULL means unused.
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
