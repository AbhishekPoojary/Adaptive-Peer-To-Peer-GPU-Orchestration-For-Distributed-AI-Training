"""Lease service (ADR-003): claim, renew, complete, fail, and the expiry sweep.

This is the concurrency-critical core. Correctness rests on Postgres, not
application locking:

* **Claim** selects a job scheduled to the node ``FOR UPDATE SKIP LOCKED`` — 50
  concurrent agents contend without blocking and never double-claim; the
  partial unique index (one ACTIVE lease per job) is the last line of defense.
* **Epoch fencing** — every mutating call carries ``lease_epoch``; a write is
  accepted only when that epoch equals the job's ``current_lease_epoch`` (the
  authoritative token) *and* the lease is still ACTIVE. A zombie holder that
  lost its lease and comes back with a stale epoch is rejected and mutates
  nothing.

Reliability counters are updated here from real outcomes only: ``+1`` success on
complete, ``+1`` failure on fail and on expiry. The service flushes; the caller
commits (the claim/renew/complete/fail HTTP handlers commit; the sweep runner
commits).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.config import Settings
from orchestrator.models.job import Job, JobState
from orchestrator.models.lease import Lease, LeaseState
from orchestrator.models.node import Node
from orchestrator.services.jobs import transition_job


class LeaseNotFoundError(Exception):
    """No lease with the given id exists (404)."""


class LeaseScopeError(Exception):
    """The authenticated node does not own this lease (403)."""


class StaleEpochError(Exception):
    """The write's epoch is not the job's current epoch — a fenced-out zombie
    (409). Nothing is mutated."""


class LeaseNotActiveError(Exception):
    """The lease is not ACTIVE, so it cannot be renewed/completed/failed (409)."""


async def claim_job_for_node(
    session: AsyncSession, *, node: Node, settings: Settings
) -> Lease | None:
    """Atomically claim one job scheduled to ``node``, granting a fresh lease.

    Returns the granted ``Lease`` (job → LEASED, epoch incremented), or ``None``
    when nothing is scheduled to this node right now. Caller commits.

    ``SELECT ... FOR UPDATE SKIP LOCKED`` guarantees that under arbitrary
    concurrency each scheduled job is claimed at most once; concurrent claimers
    that lose the row simply see no work.
    """
    job = (
        await session.execute(
            select(Job)
            .where(
                Job.scheduled_node_id == node.id, Job.state == JobState.SCHEDULED
            )
            .order_by(Job.submitted_at)
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if job is None:
        return None

    # Defense in depth against "one job per node": never grant a second lease to
    # a node already holding one (the DB index guards the per-job invariant).
    already = (
        await session.execute(
            select(Lease.id).where(
                Lease.node_id == node.id, Lease.state == LeaseState.ACTIVE
            )
        )
    ).first()
    if already is not None:
        return None

    now = datetime.now(UTC)
    new_epoch = job.current_lease_epoch + 1
    job.current_lease_epoch = new_epoch
    lease = Lease(
        id=uuid.uuid4(),
        job_id=job.id,
        node_id=node.id,
        lease_epoch=new_epoch,
        state=LeaseState.ACTIVE,
        granted_at=now,
        expires_at=now + timedelta(seconds=settings.lease_ttl_seconds),
    )
    session.add(lease)
    transition_job(
        session,
        job,
        JobState.LEASED,
        message=(
            f"Lease granted to the assigned node (epoch {new_epoch}); "
            f"expires in {settings.lease_ttl_seconds}s."
        ),
        extra={"lease_id": str(lease.id), "lease_epoch": new_epoch},
        now=now,
    )
    await session.flush()
    await session.refresh(lease)
    return lease


async def _load_fenced(
    session: AsyncSession, *, lease_id: uuid.UUID, node: Node, epoch: int
) -> tuple[Lease, Job]:
    """Load a lease + job under row locks and apply the epoch fence.

    Raises the appropriate typed error; returns ``(lease, job)`` only when the
    caller is the lease owner and the epoch is current. Order of checks:
    existence → ownership → epoch fence → ACTIVE.
    """
    lease = (
        await session.execute(
            select(Lease)
            .where(Lease.id == lease_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if lease is None:
        raise LeaseNotFoundError(str(lease_id))
    if lease.node_id != node.id:
        raise LeaseScopeError(str(lease_id))

    job = (
        await session.execute(
            select(Job)
            .where(Job.id == lease.job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    # THE fence: the job's current epoch is authoritative. A stale epoch (zombie
    # holder) is rejected before anything is mutated.
    if epoch != job.current_lease_epoch or lease.lease_epoch != job.current_lease_epoch:
        raise StaleEpochError(str(lease_id))
    if lease.state is not LeaseState.ACTIVE:
        raise LeaseNotActiveError(str(lease_id))
    return lease, job


async def renew_lease(
    session: AsyncSession,
    *,
    lease_id: uuid.UUID,
    node: Node,
    epoch: int,
    settings: Settings,
) -> Lease:
    """Extend an ACTIVE lease's TTL. Epoch-fenced. Caller commits.

    A lease being renewed means the node is actively holding it — as of M4,
    that means a real trainer container is running. The *first* renewal after
    claim is also where the job makes its LEASED -> RUNNING transition (this
    reuses the existing renew endpoint rather than adding a new "mark
    running" REST call; idempotent because the second and later renewals see
    the job already in RUNNING and skip it).
    """
    lease, job = await _load_fenced(
        session, lease_id=lease_id, node=node, epoch=epoch
    )
    now = datetime.now(UTC)
    if job.state is JobState.LEASED:
        transition_job(
            session,
            job,
            JobState.RUNNING,
            message="Training started on the leaseholder.",
            extra={"lease_id": str(lease.id), "lease_epoch": lease.lease_epoch},
            now=now,
        )
    lease.renewed_at = now
    lease.expires_at = now + timedelta(seconds=settings.lease_ttl_seconds)
    await session.flush()
    return lease


def _complete_message(result: dict[str, Any] | None) -> str:
    """Plain-language completion message for the dashboard timeline (M4).

    When a real training result is present and it reports a test accuracy,
    surface it directly — that number is exactly what a user came here for.
    Otherwise fall back to the pre-M4 generic message (non-training jobs, or a
    completion with no result payload attached).
    """
    if result is not None and result.get("final_test_accuracy") is not None:
        pct = result["final_test_accuracy"] * 100
        epochs = result.get("epochs_completed") or 0
        unit = "epoch" if epochs == 1 else "epochs"
        return f"Training completed: final test accuracy {pct:.1f}% after {epochs} {unit}."
    return "Job completed successfully by the leaseholder."


def _fail_message(reason: str, result: dict[str, Any] | None) -> str:
    """Plain-language failure message for the dashboard timeline (M4).

    A real training result carrying an exit code gets the concise, specific
    phrasing; otherwise the original reason-only message (M2/M3 behaviour, and
    still exactly right for non-training failures like a Docker launch error).
    """
    if result is not None and result.get("exit_code") is not None:
        return f"Training failed: exit code {result['exit_code']}."
    return f"Job failed on the leaseholder: {reason}"


async def complete_lease(
    session: AsyncSession,
    *,
    lease_id: uuid.UUID,
    node: Node,
    epoch: int,
    result: dict[str, Any] | None = None,
) -> Lease:
    """Finish a lease successfully: job → COMPLETED, node success += 1. Fenced.

    ``result`` is the optional real training result summary (M4) reported by
    the leaseholder; when present it is persisted to ``Job.result`` verbatim
    and shapes the audit message. Never fabricated — omitted entirely when the
    caller has nothing to report.
    """
    lease, job = await _load_fenced(
        session, lease_id=lease_id, node=node, epoch=epoch
    )
    now = datetime.now(UTC)
    lease.state = LeaseState.COMPLETED
    lease.released_at = now
    if result is not None:
        job.result = result
    transition_job(
        session,
        job,
        JobState.COMPLETED,
        message=_complete_message(result),
        extra={"lease_id": str(lease.id), "lease_epoch": lease.lease_epoch},
        now=now,
    )
    await session.execute(
        update(Node)
        .where(Node.id == node.id)
        .values(lease_success_count=Node.lease_success_count + 1)
    )
    await session.flush()
    return lease


async def fail_lease(
    session: AsyncSession,
    *,
    lease_id: uuid.UUID,
    node: Node,
    epoch: int,
    reason: str,
    result: dict[str, Any] | None = None,
) -> Lease:
    """Finish a lease as failed: job → FAILED, node failure += 1. Fenced.

    ``result`` is the optional real training result summary (M4); when present
    it is persisted to ``Job.result`` verbatim and shapes the audit message.
    """
    lease, job = await _load_fenced(
        session, lease_id=lease_id, node=node, epoch=epoch
    )
    now = datetime.now(UTC)
    lease.state = LeaseState.FAILED
    lease.released_at = now
    job.failure_reason = reason
    if result is not None:
        job.result = result
    transition_job(
        session,
        job,
        JobState.FAILED,
        message=_fail_message(reason, result),
        extra={"lease_id": str(lease.id), "lease_epoch": lease.lease_epoch, "reason": reason},
        now=now,
    )
    await session.execute(
        update(Node)
        .where(Node.id == node.id)
        .values(lease_failure_count=Node.lease_failure_count + 1)
    )
    await session.flush()
    return lease


async def sweep_expired_leases(
    session: AsyncSession, *, settings: Settings
) -> int:
    """Expire every ACTIVE lease past its TTL; reassign its job. Returns count.

    Each expiry: lease → EXPIRED, the node's ``lease_failure_count`` += 1 (a
    timeout is a real reliability signal, ADR-009), and the job → REASSIGNED
    with its target cleared so a later pass re-places it under a new epoch.
    Locked ``SKIP LOCKED`` so a concurrent sweep never double-processes a lease.
    Caller commits.
    """
    now = datetime.now(UTC)
    leases = (
        await session.execute(
            select(Lease)
            .where(Lease.state == LeaseState.ACTIVE, Lease.expires_at < now)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    if not leases:
        return 0

    for lease in leases:
        job = (
            await session.execute(
                select(Job)
                .where(Job.id == lease.job_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        lease.state = LeaseState.EXPIRED
        await session.execute(
            update(Node)
            .where(Node.id == lease.node_id)
            .values(lease_failure_count=Node.lease_failure_count + 1)
        )
        if job.state in (JobState.LEASED, JobState.RUNNING):
            transition_job(
                session,
                job,
                JobState.REASSIGNED,
                message=(
                    "Lease TTL expired without renewal; job requeued for "
                    "rescheduling under a new lease epoch."
                ),
                extra={"lease_id": str(lease.id), "lease_epoch": lease.lease_epoch},
                now=now,
            )
            job.scheduled_node_id = None

    await session.flush()
    return len(leases)
