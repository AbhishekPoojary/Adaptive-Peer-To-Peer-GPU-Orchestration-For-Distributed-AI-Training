"""Enrollment token model (ADR-008, stage 1).

The operator mints a short-lived, single-use enrollment token out of band and
hands it to a new node. Only a hash of the token is ever stored — the raw
value exists exactly once, in the HTTP response that created it, and is never
persisted or logged. Single-use is enforced atomically at claim time (see
``orchestrator.services.enrollment.claim_enrollment_token``).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from orchestrator.core.db import Base


class EnrollmentToken(Base):
    """A one-time, expiring credential authorizing a single node enrollment."""

    __tablename__ = "enrollment_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # SHA-256 hex digest of the raw token. The raw token is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set exactly once, atomically, when the token is consumed. NULL means unused.
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set when an admin explicitly withdraws a token before it is used or ages
    # out (M8). Deliberately distinct from used_at and expires_at so the audit
    # trail records *which* of the three ended the token's life, rather than
    # collapsing "revoked" into a fake early expiry.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Free-text operator label (who/what this token was minted for).
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
