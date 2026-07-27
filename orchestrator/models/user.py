"""Human operator accounts (ADR-012).

Distinct from :class:`~orchestrator.models.node.Node`, which is a *machine*
identity under ADR-008. A user authenticates with a password and receives a
JWT with ``aud="user"``; a node authenticates with an Ed25519 signature and
receives one with ``aud="node"``. Neither audience is accepted where the other
is required.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from orchestrator.core.db import Base


class UserRole(enum.Enum):
    """The two real privilege levels in this system (ADR-012 §2).

    ``OPERATOR`` submits, cancels, and reads. ``ADMIN`` additionally
    administers the fleet: minting and revoking enrollment tokens.
    """

    ADMIN = "ADMIN"
    OPERATOR = "OPERATOR"


# Named PG enum type. create_type=False: created/dropped explicitly by the
# Alembic migration, not implicitly at column bind time.
_user_role_enum = SAEnum(
    UserRole,
    name="user_role",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)


class User(Base):
    """A human operator account."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    username: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    # scrypt hash in orchestrator.core.security's self-describing format
    # ("scrypt$n$r$p$salt$hash"). The plaintext password is never stored,
    # logged, or returned by any endpoint.
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[UserRole] = mapped_column(_user_role_enum, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Set to disable an account without deleting it, so the jobs it submitted
    # keep a resolvable author. A disabled user cannot log in, and any token
    # already issued to them stops working at its next privileged check.
    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def is_active(self) -> bool:
        """True iff the account may authenticate and act."""
        return self.disabled_at is None
