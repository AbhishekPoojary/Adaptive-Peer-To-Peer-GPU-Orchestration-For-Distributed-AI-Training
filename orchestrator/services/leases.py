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
from orchestrator.schemas.lease import RendezvousAssignment
from orchestrator.services.jobs import record_job_event, transition_job


def rendezvous_wiring(job: Job) -> tuple[str, str]:
    """Deterministic (network, host_alias) for a job's cohort rendezvous (M5).

    Derived purely from the job id so every rank's agent — and the orchestrator
    building the claim response — agree without extra coordination. In the dev
    co-located topology (ADR-010) the cohort's trainer containers all join
    ``network`` (a user-defined Docker bridge, so containers resolve each other
    by name) and the rank-0 / rendezvous-host container takes ``host_alias`` as
    its container name, so ``host_alias:<rendezvous_port>`` resolves to it for
    every rank. For the real multi-host phase this is replaced by the peer's
    Tailscale overlay address (see docs/adr/ADR-005-addendum.md)."""
    short = job.id.hex[:12]
    network = f"gpuorch-rdzv-{short}"
    host_alias = f"{network}-r0"
    return network, host_alias


def rendezvous_assignment(
    job: Job, lease: Lease, *, settings: Settings
) -> RendezvousAssignment:
    """Build the rank/world_size/endpoint an agent needs to launch a claimed
    lease (M5). Pure over the job + lease + config."""
    world_size = int(job.spec.get("world_size", 1) or 1)
    network, host_alias = rendezvous_wiring(job)
    return RendezvousAssignment(
        rank=lease.rank,
        world_size=world_size,
        is_rendezvous_host=lease.rank == 0,
        backend=settings.training_backend,
        endpoint=f"{host_alias}:{settings.rendezvous_port}",
        rdzv_id=f"{job.id.hex}-{lease.lease_epoch}",
        network=network if world_size > 1 else "",
        host_alias=host_alias if world_size > 1 else "",
        max_restarts=settings.torchrun_max_restarts,
    )


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
    """Atomically claim this node's assigned rank slot, activating its lease.

    Returns the granted ``Lease`` (its PENDING cohort slot flipped ACTIVE), or
    ``None`` when nothing is assigned to this node right now. Caller commits.

    The scheduler creates a cohort as N PENDING leases at a pre-minted attempt
    epoch (``services.scheduling.place_job_cohort``); a claim does *not* mint an
    epoch, it activates the one slot reserved for this node. ``SELECT ... FOR
    UPDATE SKIP LOCKED`` on that PENDING row guarantees that under arbitrary
    concurrency each slot is activated at most once; concurrent claimers for the
    same node that lose the row simply see no work. The first rank of a cohort
    to claim flips the job SCHEDULED → LEASED; later ranks find it already
    LEASED/RUNNING and only record that they joined.
    """
    lease = (
        await session.execute(
            select(Lease)
            .where(Lease.node_id == node.id, Lease.state == LeaseState.PENDING)
            .order_by(Lease.lease_epoch)
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if lease is None:
        return None

    # Lock the job so cohort members serialise on the SCHEDULED → LEASED flip.
    job = (
        await session.execute(
            select(Job)
            .where(Job.id == lease.job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    # Defensive: a PENDING slot from a superseded attempt (its epoch is no longer
    # the job's current one) is never activated — the sweep clears such rows, but
    # never trust that it already has.
    if lease.lease_epoch != job.current_lease_epoch:
        return None

    now = datetime.now(UTC)
    lease.state = LeaseState.ACTIVE
    lease.granted_at = now
    lease.expires_at = now + timedelta(seconds=settings.lease_ttl_seconds)

    grant_extra = {
        "lease_id": str(lease.id),
        "lease_epoch": lease.lease_epoch,
        "rank": lease.rank,
    }
    if job.state is JobState.SCHEDULED:
        transition_job(
            session,
            job,
            JobState.LEASED,
            message=(
                f"Lease granted for rank {lease.rank} (epoch {lease.lease_epoch}); "
                f"expires in {settings.lease_ttl_seconds}s."
            ),
            extra=grant_extra,
            now=now,
        )
    else:
        # A sibling rank already flipped the job LEASED/RUNNING; record this rank
        # joining as a plain audit event (no state change).
        record_job_event(
            session,
            job,
            from_state=job.state,
            to_state=job.state,
            message=f"Rank {lease.rank} lease granted (epoch {lease.lease_epoch}).",
            extra=grant_extra,
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


async def _release_cohort_siblings(
    session: AsyncSession,
    *,
    job: Job,
    epoch: int,
    exclude_lease_ids: set[uuid.UUID],
    now: datetime,
) -> int:
    """Release every still-non-terminal sibling lease of one attempt (ADR-005).

    When one rank of a cohort fails or times out, the whole attempt is doomed
    (M5 tears the attempt down rather than doing in-flight single-rank
    replacement — that is M6's checkpoint territory). The siblings are set
    RELEASED, not FAILED/EXPIRED: their nodes did nothing wrong, so — exactly
    like a cancellation — their reliability counters are untouched. Returns how
    many were released. Rows are locked ``FOR UPDATE`` so a concurrent sweep
    never double-processes them.
    """
    siblings = (
        await session.execute(
            select(Lease)
            .where(
                Lease.job_id == job.id,
                Lease.lease_epoch == epoch,
                Lease.id.not_in(exclude_lease_ids),
                Lease.state.in_((LeaseState.PENDING, LeaseState.ACTIVE)),
            )
            .with_for_update()
        )
    ).scalars().all()
    for sibling in siblings:
        sibling.state = LeaseState.RELEASED
        sibling.released_at = now
    return len(siblings)


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


def _result_has_metrics(result: dict[str, Any] | None) -> bool:
    """True iff a training result actually carries measured numbers.

    A rank>0 DDP worker completes its lease with an all-null result payload (it
    trains silently — only rank 0 reports real metrics). This distinguishes that
    placeholder from rank 0's real result so the cohort's job.result is populated
    from the rank that actually measured accuracy/loss, whatever order the
    cohort's completes arrive in."""
    if result is None:
        return False
    return result.get("final_test_accuracy") is not None or result.get("final_loss") is not None


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
    # Only rank 0 (the rendezvous host) carries the real training result; a
    # rank>0 agent trains silently and reports an all-null payload. Populate
    # job.result from the rank that actually measured metrics, and never clobber
    # a real result with a null one — whatever order the cohort's completes
    # arrive in.
    if result is not None and (job.result is None or _result_has_metrics(result)):
        job.result = result
    # Every rank that finishes its own work is a real success for its node.
    await session.execute(
        update(Node)
        .where(Node.id == node.id)
        .values(lease_success_count=Node.lease_success_count + 1)
    )
    finalize_extra = {
        "lease_id": str(lease.id),
        "lease_epoch": lease.lease_epoch,
        "rank": lease.rank,
    }
    if job.state in (JobState.LEASED, JobState.RUNNING):
        # First cohort member to complete flips the job COMPLETED. The epoch is
        # not bumped, so siblings still ACTIVE remain fence-valid and can finish
        # their own leases afterward.
        transition_job(
            session,
            job,
            JobState.COMPLETED,
            message=_complete_message(job.result),
            extra=finalize_extra,
            now=now,
        )
    else:
        # A sibling already finished the job; this rank just finalises its lease.
        record_job_event(
            session,
            job,
            from_state=job.state,
            to_state=job.state,
            message=f"Rank {lease.rank} finished (cohort already {job.state.value}).",
            extra=finalize_extra,
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
    if result is not None and job.result is None:
        job.result = result
    # The failing rank's node earns a real reliability failure.
    await session.execute(
        update(Node)
        .where(Node.id == node.id)
        .values(lease_failure_count=Node.lease_failure_count + 1)
    )
    # One rank failing dooms the whole attempt (M5): release the siblings so the
    # cohort stops, and end the job. A reported training failure is TERMINAL
    # (ADR-005: failures surface instead of being masked by endless retry, and
    # this preserves M4's single-rank fail → FAILED). A lease that merely
    # *expired* (node dropped) is the retryable case and goes REASSIGNED via the
    # sweep instead.
    finalize_extra = {
        "lease_id": str(lease.id),
        "lease_epoch": lease.lease_epoch,
        "rank": lease.rank,
        "reason": reason,
    }
    if job.state in (JobState.LEASED, JobState.RUNNING):
        await _release_cohort_siblings(
            session, job=job, epoch=lease.lease_epoch, exclude_lease_ids={lease.id}, now=now
        )
        transition_job(
            session,
            job,
            JobState.FAILED,
            message=_fail_message(reason, result),
            extra=finalize_extra,
            now=now,
        )
        job.scheduled_node_id = None
        job.rendezvous_node_id = None
    else:
        # A sibling already ended the job; this rank just finalises its lease.
        record_job_event(
            session,
            job,
            from_state=job.state,
            to_state=job.state,
            message=f"Rank {lease.rank} reported failure (cohort already {job.state.value}).",
            extra=finalize_extra,
        )
    await session.flush()
    return lease


#: Job states from which an attempt whose cohort lost a member is reassignable.
#: SCHEDULED is included so a cohort whose PENDING slots expire before every rank
#: claims (a member that never showed up) is retried, not left stuck.
_REASSIGNABLE = (JobState.LEASED, JobState.RUNNING, JobState.SCHEDULED)


async def sweep_expired_leases(
    session: AsyncSession, *, settings: Settings
) -> int:
    """Expire overdue cohort leases and reassign the whole attempt. Returns the
    number of leases expired. Caller commits.

    "Overdue" is an ACTIVE lease past its TTL (a rank that stopped renewing) *or*
    a PENDING slot past its TTL (a rank that never claimed) — both mean a cohort
    member failed to make progress. Each overdue lease at the job's current
    epoch → EXPIRED with the node's ``lease_failure_count`` += 1 (a timeout is a
    real reliability signal, ADR-009). The rest of that attempt's cohort is then
    torn down (siblings RELEASED, no reliability hit — the drop was not their
    fault) and the job → REASSIGNED with its cohort/rendezvous wiring cleared, so
    a later pass re-selects N nodes under a fresh epoch (ADR-005: one rank
    dropping fails the whole attempt in M5; in-flight single-rank replacement is
    M6). Locked ``SKIP LOCKED`` so a concurrent sweep never double-processes.
    """
    now = datetime.now(UTC)
    overdue = (
        await session.execute(
            select(Lease)
            .where(
                Lease.state.in_((LeaseState.ACTIVE, LeaseState.PENDING)),
                Lease.expires_at < now,
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    if not overdue:
        return 0

    # Group by job: each doomed attempt is torn down and reassigned exactly once,
    # even when several of its ranks time out in the same sweep.
    by_job: dict[uuid.UUID, list[Lease]] = {}
    for lease in overdue:
        by_job.setdefault(lease.job_id, []).append(lease)

    expired_count = 0
    for job_id, overdue_leases in by_job.items():
        job = (
            await session.execute(
                select(Job)
                .where(Job.id == job_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

        current_overdue = [
            lease for lease in overdue_leases
            if lease.lease_epoch == job.current_lease_epoch
        ]
        for lease in overdue_leases:
            lease.state = LeaseState.EXPIRED
            expired_count += 1
            # Only a timeout at the current epoch is this node's reliability
            # failure; a leftover stale-epoch row (defensive) is just cleaned up.
            if lease.lease_epoch == job.current_lease_epoch:
                await session.execute(
                    update(Node)
                    .where(Node.id == lease.node_id)
                    .values(lease_failure_count=Node.lease_failure_count + 1)
                )

        if not current_overdue:
            continue
        await _release_cohort_siblings(
            session,
            job=job,
            epoch=job.current_lease_epoch,
            exclude_lease_ids={lease.id for lease in current_overdue},
            now=now,
        )
        if job.state in _REASSIGNABLE:
            n_timed_out = len(current_overdue)
            transition_job(
                session,
                job,
                JobState.REASSIGNED,
                message=(
                    f"{n_timed_out} cohort lease(s) expired without progress; "
                    "the whole attempt was torn down and requeued for "
                    "rescheduling under a new lease epoch."
                ),
                extra={
                    "expired_ranks": sorted(lease.rank for lease in current_overdue),
                    "lease_epoch": job.current_lease_epoch,
                },
                now=now,
            )
            job.scheduled_node_id = None
            job.rendezvous_node_id = None

    await session.flush()
    return expired_count
