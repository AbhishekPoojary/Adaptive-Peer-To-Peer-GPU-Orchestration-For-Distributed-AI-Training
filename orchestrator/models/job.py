"""Job and JobEvent models plus the job state machine (M2, ADR-003).

A ``Job`` is a unit of training work submitted to the control plane. Its
lifecycle is an explicit state machine (``JobState``); every transition is
persisted as a ``JobEvent`` row so the dashboard's plain-language timeline
(M7) can be rendered from a real audit trail rather than reconstructed after
the fact. ``JobEvent.detail`` always carries a human-readable ``message``.

Nothing here fabricates data: ``current_lease_epoch`` starts at 0 (no lease
yet) and is only advanced by a real claim; ``failure_reason`` is NULL unless a
real failure recorded it.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from orchestrator.core.db import Base

if TYPE_CHECKING:
    from orchestrator.models.lease import Lease


class JobState(enum.Enum):
    """Lifecycle state of a job.

    ``QUEUED → SCHEDULED → LEASED → RUNNING → {COMPLETED, FAILED}``. A lease
    that expires sends the job to ``REASSIGNED``, from which the scheduler
    re-places it (``REASSIGNED → SCHEDULED``) under a fresh lease epoch.
    ``COMPLETED``, ``FAILED`` and ``CANCELLED`` are terminal.
    """

    QUEUED = "QUEUED"
    SCHEDULED = "SCHEDULED"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REASSIGNED = "REASSIGNED"
    CANCELLED = "CANCELLED"


# Named PG enum type. create_type=False: created/dropped explicitly by the
# Alembic migration, mirroring the node_status pattern.
_job_state_enum = SAEnum(
    JobState,
    name="job_state",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)


#: Terminal states — a job here never transitions again.
TERMINAL_STATES: frozenset[JobState] = frozenset(
    {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
)

#: The only legal state transitions. Anything not listed is rejected by
#: ``orchestrator.services.jobs.transition_job`` (illegal transitions are a
#: programming/protocol error, surfaced as HTTP 409, never silently applied).
VALID_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.SCHEDULED, JobState.CANCELLED}),
    JobState.SCHEDULED: frozenset(
        # REASSIGNED (M5): a scheduled cohort whose PENDING rank slots expire
        # before every rank claims is torn down and re-placed, not left stuck.
        {JobState.LEASED, JobState.QUEUED, JobState.REASSIGNED, JobState.CANCELLED}
    ),
    JobState.LEASED: frozenset(
        {
            JobState.RUNNING,
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.REASSIGNED,
            JobState.CANCELLED,
        }
    ),
    JobState.RUNNING: frozenset(
        {
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.REASSIGNED,
            JobState.CANCELLED,
        }
    ),
    JobState.REASSIGNED: frozenset({JobState.SCHEDULED, JobState.CANCELLED}),
    JobState.COMPLETED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
}


def is_terminal(state: JobState) -> bool:
    """True iff ``state`` is a terminal job state."""
    return state in TERMINAL_STATES


def can_transition(from_state: JobState, to_state: JobState) -> bool:
    """True iff ``from_state → to_state`` is a legal transition."""
    return to_state in VALID_TRANSITIONS[from_state]


class Job(Base):
    """A training job submitted to the orchestrator."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Validated training spec (dataset, model, epochs, ...). Stored as-submitted;
    # the API layer validates it against JobSpec before it lands here.
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Registered scheduler name (round_robin | least_loaded in M2). Validated
    # against the live registry at submit time.
    scheduler_name: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[JobState] = mapped_column(
        _job_state_enum, nullable=False, default=JobState.QUEUED
    )
    # Monotonic per-job lease epoch. 0 = never leased; advanced by exactly 1 on
    # each real claim. The fencing token from ADR-003: agent writes carrying a
    # stale epoch are rejected.
    current_lease_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    # How many attempts have ended in a *reported* trainer failure (ADR-005
    # addendum 2). Bounds retry: past MAX_JOB_FAILURE_RETRIES the job fails
    # terminally instead of walking a broken spec across the whole fleet.
    #
    # Deliberately not derived from current_lease_epoch, which also advances
    # for dropped-node reassignments — a job whose peers keep vanishing would
    # otherwise exhaust its failure budget having never actually failed.
    failed_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    # Node this job is currently placed on (set at SCHEDULED, consumed at claim,
    # cleared on reassignment). NULL while QUEUED/terminal. For a multi-rank
    # (world_size>1) cohort this is the rank-0 / rendezvous-host node.
    scheduled_node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("nodes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # The c10d rendezvous host for the current attempt (ADR-005): the cohort
    # member the orchestrator scores highest-reliability. NULL until a cohort is
    # scheduled; cleared on reassignment. SET NULL on node delete so a completed
    # job's audit trail survives node removal. Rank 0 is always this node.
    rendezvous_node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("nodes.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Free-text submitter identity (real user auth arrives in M8).
    submitted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Populated only by a real failure; NULL otherwise.
    failure_reason: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Populated only when a real training run reported an outcome (M4, ADR-007
    # execution): {"final_loss": f, "final_test_accuracy": f, "epochs_completed":
    # n, "exit_code": n, "device": "cuda"|"cpu"}. NULL until the leaseholder's
    # complete/fail call carries a real result summary — never fabricated, and
    # individual fields may be null within it if the trainer never got that far
    # (e.g. a Docker launch failure before any epoch ran).
    result: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )

    events: Mapped[list[JobEvent]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="JobEvent.ts, JobEvent.id",
    )
    leases: Mapped[list[Lease]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="Lease.lease_epoch",
    )


class JobEvent(Base):
    """One recorded job state transition — the audit trail (M7 timeline source).

    ``detail`` always carries a human-readable ``message`` key plus any
    structured context (node id, lease epoch, failure reason). ``from_state`` is
    NULL for the very first event (job creation into QUEUED).
    """

    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    from_state: Mapped[JobState | None] = mapped_column(_job_state_enum, nullable=True)
    to_state: Mapped[JobState] = mapped_column(_job_state_enum, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    job: Mapped[Job] = relationship(back_populates="events")
