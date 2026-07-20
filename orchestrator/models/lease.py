"""Lease model (M2, ADR-003 — the concurrency-critical core).

A ``Lease`` is a time-bounded claim by exactly one node on exactly one job.
Concurrency safety rests on two Postgres-level guarantees, not application
locking:

* Claims use ``SELECT ... FOR UPDATE SKIP LOCKED`` so concurrent agents never
  double-claim or block on each other.
* A partial unique index (``one ACTIVE lease per job``) makes a second ACTIVE
  lease on a job physically impossible — defense in depth behind the claim
  logic.

Each lease carries a monotonic ``lease_epoch``; agent writes fenced against the
job's current epoch reject stale ("zombie") holders (see
``orchestrator.services.leases``).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    func,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from orchestrator.core.db import Base

if TYPE_CHECKING:
    from orchestrator.models.job import Job


class LeaseState(enum.Enum):
    """Lifecycle state of a lease.

    ``ACTIVE`` leases are renewable and hold the job. ``EXPIRED`` (TTL elapsed,
    swept), ``RELEASED`` (cancelled out from under it), ``COMPLETED`` and
    ``FAILED`` are terminal. ``PENDING`` is reserved for a future two-phase
    grant; M2 grants leases ``ACTIVE`` directly.
    """

    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    RELEASED = "RELEASED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


_lease_state_enum = SAEnum(
    LeaseState,
    name="lease_state",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)


class Lease(Base):
    """A time-bounded, epoch-fenced claim by one node on one job."""

    __tablename__ = "leases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Matches the job's current_lease_epoch at grant time. Monotonic per job.
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[LeaseState] = mapped_column(_lease_state_enum, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    renewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    job: Mapped[Job] = relationship(back_populates="leases")

    __table_args__ = (
        # Defense in depth: at most one ACTIVE lease per job, enforced by the
        # database itself. Even a logic bug cannot double-assign a job.
        Index(
            "uq_one_active_lease_per_job",
            "job_id",
            unique=True,
            postgresql_where=text("state = 'ACTIVE'"),
        ),
    )
