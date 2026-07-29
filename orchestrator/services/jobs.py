"""Job service: creation, the audited state machine, cancellation, read views.

Every state change goes through :func:`transition_job`, which refuses an
illegal transition (raising :class:`IllegalTransitionError`, surfaced as HTTP
409) and writes a :class:`JobEvent` with a human-readable ``message`` — so the
audit trail is a byproduct of transitioning, never an afterthought that can
drift from reality.

Services here flush but never commit; the caller owns the transaction (matching
the M1 convention in ``services/nodes.py``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from orchestrator.models.job import (
    Job,
    JobEvent,
    JobState,
    can_transition,
    is_terminal,
)
from orchestrator.models.lease import Lease, LeaseState
from orchestrator.schemas.job import (
    JobDetailResponse,
    JobEventOut,
    JobSubmitRequest,
    JobSummary,
    LeaseOut,
)


class IllegalTransitionError(Exception):
    """An attempt to move a job through a transition its state machine forbids."""

    def __init__(self, from_state: JobState, to_state: JobState) -> None:
        super().__init__(f"illegal job transition {from_state.value} -> {to_state.value}")
        self.from_state = from_state
        self.to_state = to_state


def record_job_event(
    session: AsyncSession,
    job: Job,
    *,
    from_state: JobState | None,
    to_state: JobState,
    message: str,
    extra: dict[str, Any] | None = None,
) -> JobEvent:
    """Append one audit event. ``detail`` always includes ``message``.

    Uses ``session.add`` (not ``job.events.append``) so no async lazy-load of
    the events collection is triggered.
    """
    detail: dict[str, Any] = {"message": message}
    if extra:
        detail.update(extra)
    event = JobEvent(
        job_id=job.id, from_state=from_state, to_state=to_state, detail=detail
    )
    session.add(event)
    return event


def transition_job(
    session: AsyncSession,
    job: Job,
    to_state: JobState,
    *,
    message: str,
    extra: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """Validate and apply a job state transition, recording a JobEvent.

    Stamps ``started_at`` the first time the job is placed (LEASED/RUNNING) and
    ``finished_at`` on any terminal state. Raises :class:`IllegalTransitionError`
    if the transition is not allowed — the job is left untouched.
    """
    from_state = job.state
    if not can_transition(from_state, to_state):
        raise IllegalTransitionError(from_state, to_state)

    moment = now or datetime.now(UTC)
    if to_state in (JobState.LEASED, JobState.RUNNING) and job.started_at is None:
        job.started_at = moment
    if is_terminal(to_state):
        job.finished_at = moment

    job.state = to_state
    record_job_event(
        session, job, from_state=from_state, to_state=to_state, message=message, extra=extra
    )


async def create_job(
    session: AsyncSession,
    *,
    req: JobSubmitRequest,
    scheduler_name: str,
    submitted_by: str,
) -> Job:
    """Create a QUEUED job with its first audit event. Caller commits.

    ``scheduler_name`` is the already-validated effective strategy (the request
    value or the server default). ``submitted_by`` is the *authenticated*
    username passed down by the API layer (ADR-012 §4) — deliberately a
    required argument rather than a read of ``req.submitted_by``, so this
    cannot silently fall back to a client-asserted identity.
    """
    job = Job(
        spec=req.spec.model_dump(),
        scheduler_name=scheduler_name,
        state=JobState.QUEUED,
        submitted_by=submitted_by,
    )
    session.add(job)
    await session.flush()
    record_job_event(
        session,
        job,
        from_state=None,
        to_state=JobState.QUEUED,
        message=f"Job submitted by {submitted_by}; queued for scheduling.",
        extra={"scheduler_name": scheduler_name},
    )
    await session.flush()
    await session.refresh(job)
    return job


async def record_resume_event(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    from_step: int,
    from_epoch: int,
    checkpoint_key: str | None = None,
) -> bool:
    """Append a plain-language "resumed from checkpoint" event to a job's
    timeline (M6, ADR-006). Fed by the WebSocket stream when the trainer reports
    it loaded a checkpoint and continued from a step > 0. Records a same-state
    audit event (no transition) so the M7 dashboard timeline reads
    ``… → reassigned → resumed from checkpoint step N``. Returns ``False`` (a
    no-op) if the job is unknown. Caller commits.
    """
    job = await session.get(Job, job_id)
    if job is None:
        return False
    extra: dict[str, Any] = {"resumed_from_step": from_step, "resumed_from_epoch": from_epoch}
    if checkpoint_key is not None:
        extra["checkpoint_key"] = checkpoint_key
    record_job_event(
        session,
        job,
        from_state=job.state,
        to_state=job.state,
        message=f"Resumed from checkpoint at step {from_step} (epoch {from_epoch}).",
        extra=extra,
    )
    await session.flush()
    return True


class JobNotFoundError(Exception):
    """The referenced job does not exist."""


async def cancel_job(session: AsyncSession, *, job_id: uuid.UUID) -> Job:
    """Cancel a non-terminal job, releasing any ACTIVE lease it holds.

    Releasing the lease (state RELEASED) is not a node failure — reliability
    counters are untouched. Raises :class:`JobNotFoundError` (404) or
    :class:`IllegalTransitionError` (409, already terminal). Caller commits.
    """
    job = await session.get(Job, job_id)
    if job is None:
        raise JobNotFoundError(str(job_id))

    now = datetime.now(UTC)
    # Release every non-terminal lease of the job — a multi-rank cohort has one
    # per rank (PENDING slots not yet claimed and ACTIVE ranks alike). Releasing
    # (state RELEASED) is not a node failure, so reliability counters are
    # untouched.
    non_terminal = (
        await session.execute(
            select(Lease).where(
                Lease.job_id == job.id,
                Lease.state.in_((LeaseState.PENDING, LeaseState.ACTIVE)),
            )
        )
    ).scalars().all()
    for lease in non_terminal:
        lease.state = LeaseState.RELEASED
        lease.released_at = now

    transition_job(
        session,
        job,
        JobState.CANCELLED,
        message="Job cancelled; any active or pending cohort leases were released.",
        now=now,
    )
    return job


# --- Read views --------------------------------------------------------------


def _lease_out(lease: Lease) -> LeaseOut:
    return LeaseOut(
        id=lease.id,
        job_id=lease.job_id,
        node_id=lease.node_id,
        lease_epoch=lease.lease_epoch,
        rank=lease.rank,
        state=lease.state.value,
        granted_at=lease.granted_at,
        expires_at=lease.expires_at,
        renewed_at=lease.renewed_at,
        released_at=lease.released_at,
    )


def _summary(job: Job) -> JobSummary:
    return JobSummary(
        id=job.id,
        spec=job.spec,
        scheduler_name=job.scheduler_name,
        state=job.state.value,
        current_lease_epoch=job.current_lease_epoch,
        failed_attempt_count=job.failed_attempt_count,
        scheduled_node_id=job.scheduled_node_id,
        submitted_by=job.submitted_by,
        submitted_at=job.submitted_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        failure_reason=job.failure_reason,
        result=job.result,
    )


async def list_jobs(session: AsyncSession) -> list[JobSummary]:
    """Every job, newest submission first."""
    rows = (
        await session.execute(select(Job).order_by(Job.submitted_at.desc()))
    ).scalars().all()
    return [_summary(j) for j in rows]


async def get_job_detail(
    session: AsyncSession, *, job_id: uuid.UUID
) -> JobDetailResponse | None:
    """One job with its full audit trail and leases, or ``None`` if unknown."""
    job = (
        await session.execute(
            select(Job)
            .where(Job.id == job_id)
            .options(selectinload(Job.events), selectinload(Job.leases))
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if job is None:
        return None

    events = [
        JobEventOut(
            ts=e.ts,
            from_state=e.from_state.value if e.from_state is not None else None,
            to_state=e.to_state.value,
            detail=e.detail,
        )
        for e in job.events
    ]
    leases = [_lease_out(lease) for lease in job.leases]
    return JobDetailResponse(**_summary(job).model_dump(), events=events, leases=leases)
