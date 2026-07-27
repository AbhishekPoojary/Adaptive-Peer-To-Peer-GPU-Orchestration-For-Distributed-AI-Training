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

M5 generalises this to N-rank training cohorts (ADR-005). A world_size=N job's
attempt is N leases that all share one ``lease_epoch`` (minted once, at schedule
time) and differ only by ``rank`` (0..N-1). The unique index is correspondingly
generalised to *one non-terminal lease per (job, rank)*: a cohort holds N
concurrent ACTIVE leases legitimately, but never two for the same rank. A
single-rank (world_size=1) job is simply a one-member cohort at rank 0, so the
M2 path is unchanged.
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

    Non-terminal: ``PENDING`` — a cohort slot the scheduler reserved for a node
    that has not claimed it yet (M5) — and ``ACTIVE``, a claimed, renewable
    lease the node is really working under.

    The terminal states record *what actually happened*, and the distinction
    between them is the reliability model itself (ADR-009; see
    ``docs/adr/ADR-003-addendum.md`` for the expiry semantics):

    * ``COMPLETED`` — the node finished the work. Its node's success.
    * ``FAILED`` — the node took the work and reported a real failure. Its
      node's failure.
    * ``EXPIRED`` — an **ACTIVE** lease passed its TTL without renewal: the node
      took work on and stopped making progress. Its node's failure.
    * ``UNCLAIMED`` — a **PENDING** slot passed its TTL without ever being
      claimed. The scheduler *offered* work that was never picked up, so no node
      ever took it on: the offer is withdrawn and the job rescheduled, and the
      node's reliability is untouched. Distinct from ``EXPIRED`` precisely
      because "never picked up the offer" is not "dropped work it had taken".
    * ``RELEASED`` — the lease was torn down out from under a blameless node
      (job cancellation, or a cohort sibling failing). No reliability effect.
    """

    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    UNCLAIMED = "UNCLAIMED"
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
    # Every lease in one attempt's cohort shares this epoch (ADR-005 + ADR-003).
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    # Rank slot 0..world_size-1 within the job's attempt. 0 for a single-rank
    # (world_size=1) job — so the M2 path is exactly rank 0. Rank 0 is always the
    # rendezvous host (ADR-005: highest-reliability cohort member).
    rank: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
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
        # Defense in depth: at most one *non-terminal* (PENDING or ACTIVE) lease
        # per (job, rank), enforced by the database itself. A cohort's N ranks
        # each hold their own ACTIVE lease legitimately, but a rank is never
        # double-assigned and a PENDING slot is never duplicated — even a logic
        # bug cannot violate it (ADR-003 defense in depth, generalised for M5).
        Index(
            "uq_active_lease_per_job_rank",
            "job_id",
            "rank",
            unique=True,
            postgresql_where=text("state IN ('PENDING', 'ACTIVE')"),
        ),
    )
