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
complete, ``+1`` failure on fail and on the expiry of an *ACTIVE* lease. A
PENDING slot that was offered and never claimed is **not** a node failure — it
ends ``UNCLAIMED`` with reliability untouched (see
:func:`sweep_expired_leases` and ``docs/adr/ADR-003-addendum.md``). The service
flushes; the caller commits (the claim/renew/complete/fail HTTP handlers commit;
the sweep runner commits).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.config import Settings
from orchestrator.core.metrics import lease_expiries_total, lease_offers_unclaimed_total
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


def _fail_message(
    reason: str, result: dict[str, Any] | None, *, attempts: int = 1
) -> str:
    """Plain-language failure message for the dashboard timeline (M4).

    A real training result carrying an exit code gets the concise, specific
    phrasing; otherwise the original reason-only message (M2/M3 behaviour, and
    still exactly right for non-training failures like a Docker launch error).

    ``attempts`` is stated whenever the job was retried (ADR-005 addendum 2), so
    a failure that took a minute and three peers to arrive at does not read like
    a single unlucky run.
    """
    if result is not None and result.get("exit_code") is not None:
        detail = f"Training failed: exit code {result['exit_code']}."
    else:
        detail = f"Job failed on the leaseholder: {reason}"
    if attempts > 1:
        return f"{detail} Gave up after {attempts} attempts on different peers."
    return detail


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
    max_failure_retries: int = 0,
    failed_attempt_backoff_seconds: float = 0.0,
) -> Lease:
    """Finish a lease as failed. Node failure += 1, job retried or ended. Fenced.

    ``result`` is the optional real training result summary (M4); when present
    it is persisted to ``Job.result`` verbatim and shapes the audit message.

    One rank failing dooms the whole *attempt* (M5): the cohort siblings are
    released so it stops. What happens to the *job* then is bounded retry
    (ADR-005 addendum 2). ADR-005 originally made any reported failure terminal,
    to stop a broken job spec walking the fleet. That is still the concern, but
    it wrongly assumed every trainer failure is a property of the job — an OOM
    kill on a 4 GB laptop GPU is a property of the *machine*, and another peer
    may well succeed. So:

    * under ``max_failure_retries``, the job goes ``REASSIGNED`` — the same
      retry path a dropped node takes — and the failing node is put in a short
      scheduling backoff so the retry prefers different hardware;
    * at or past it, the job goes ``FAILED`` with an audit message stating how
      many attempts were made, so a bounded-but-slow failure is never
      mysterious.

    ``max_failure_retries=0`` (the default here) reproduces the strict pre-M11
    behaviour exactly; the API layer passes the configured value.
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
    # The failing rank's node earns a real reliability failure, whether or not
    # the job is retried: a trainer really did die on it, and that is exactly
    # the earned signal ADR-009 ranks on.
    await session.execute(
        update(Node)
        .where(Node.id == node.id)
        .values(lease_failure_count=Node.lease_failure_count + 1)
    )
    finalize_extra = {
        "lease_id": str(lease.id),
        "lease_epoch": lease.lease_epoch,
        "rank": lease.rank,
        "reason": reason,
    }
    if job.state in (JobState.LEASED, JobState.RUNNING):
        job.failed_attempt_count += 1
        attempts = job.failed_attempt_count
        may_retry = attempts <= max_failure_retries

        if may_retry and failed_attempt_backoff_seconds > 0:
            # Steer the retry away from the node that just killed a trainer. A
            # short window, not an exclusion — on a single-node fleet the job
            # must still be able to retry where it is, because "no other peer
            # exists" should delay a retry rather than cancel it.
            await session.execute(
                update(Node)
                .where(Node.id == node.id)
                .values(
                    scheduling_backoff_until=now
                    + timedelta(seconds=failed_attempt_backoff_seconds)
                )
            )

        await _release_cohort_siblings(
            session, job=job, epoch=lease.lease_epoch, exclude_lease_ids={lease.id}, now=now
        )
        if may_retry:
            transition_job(
                session,
                job,
                JobState.REASSIGNED,
                message=(
                    f"Attempt {attempts} failed on {node.name} ({reason}); "
                    f"retrying on another peer "
                    f"({max_failure_retries - attempts + 1} retries left)."
                ),
                extra={
                    **finalize_extra,
                    "failed_attempt_count": attempts,
                    "max_failure_retries": max_failure_retries,
                    "node_name": node.name,
                },
                now=now,
            )
        else:
            transition_job(
                session,
                job,
                JobState.FAILED,
                message=_fail_message(reason, result, attempts=attempts),
                extra={
                    **finalize_extra,
                    "failed_attempt_count": attempts,
                    "max_failure_retries": max_failure_retries,
                },
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


async def reassign_job_attempt(
    session: AsyncSession,
    *,
    job: Job,
    failed_leases: list[Lease],
    now: datetime,
    message: str,
    extra: dict[str, Any] | None = None,
    unclaimed_leases: list[Lease] | None = None,
    unclaimed_backoff_seconds: float = 0.0,
) -> bool:
    """Tear down a doomed attempt and requeue the job — the single reassignment
    code path shared by the TTL-expiry sweep (:func:`sweep_expired_leases`) and
    the M6 φ-accrual failure detector
    (``services.failure_detection.run_failure_detection_pass``). Two triggers,
    one implementation — never duplicated.

    ``failed_leases`` are current-epoch leases whose nodes are at fault (an
    ACTIVE lease timed out, or the node was declared offline by the φ-accrual
    detector): each → ``EXPIRED`` with the node's ``lease_failure_count`` += 1 (a
    real reliability signal, ADR-009). Their expiry is the retryable path —
    distinct from an agent-reported ``fail_lease`` (which is terminal).

    ``unclaimed_leases`` are current-epoch PENDING slots that were *offered* and
    never claimed: each → ``UNCLAIMED`` with **no** reliability effect. Nobody
    took that work on, so nobody can have dropped it — penalising it would
    manufacture failures for a healthy node whose agent was merely busy or
    slow to poll (ADR-003 addendum). The two lists are kept separate rather than
    inferred from ``Lease.state`` inside this function so each caller states its
    own fault attribution explicitly.

    The rest of the attempt's cohort is ``RELEASED`` (no fault, reliability
    untouched). The job → ``REASSIGNED`` with cohort/rendezvous wiring cleared,
    so a later scheduler pass re-selects nodes at a fresh epoch — the
    ``ONLINE``-only hard filter (ADR-009) keeps the just-failed node out until it
    heartbeats again.

    Returns ``True`` iff the job was moved to ``REASSIGNED`` (``False`` when it
    was no longer in a reassignable state — e.g. a sibling already ended it).
    Caller commits.
    """
    unclaimed = unclaimed_leases or []
    for lease in failed_leases:
        lease.state = LeaseState.EXPIRED
        # A timeout / detected drop at the current epoch is this node's real
        # reliability failure (matches the historical sweep behaviour).
        await session.execute(
            update(Node)
            .where(Node.id == lease.node_id)
            .values(lease_failure_count=Node.lease_failure_count + 1)
        )
    for lease in unclaimed:
        # Terminal, retryable, and blameless: the offer lapsed. No counter moves,
        # and UNCLAIMED is excluded from the lease-history reliability inputs the
        # adaptive scheduler reads (services.scheduling._reliability_inputs).
        lease.state = LeaseState.UNCLAIMED
    if unclaimed and unclaimed_backoff_seconds > 0:
        # M7.1c: skip these nodes for a moment. They are healthy by every other
        # measure — heartbeating, idle, blameless — which is precisely why the
        # next pass would otherwise re-offer the same work to the same node and
        # watch it lapse again. Observed in the wild: 7 wasted epochs in 108s
        # against one node whose agent was stuck.
        #
        # A timestamp rather than a counter, so it expires on its own with no
        # reset path to forget: a node that starts claiming again is simply
        # eligible once the moment passes. Still not a reliability signal.
        await session.execute(
            update(Node)
            .where(Node.id.in_([lease.node_id for lease in unclaimed]))
            .values(
                scheduling_backoff_until=now + timedelta(seconds=unclaimed_backoff_seconds)
            )
        )
    await _release_cohort_siblings(
        session,
        job=job,
        epoch=job.current_lease_epoch,
        exclude_lease_ids={lease.id for lease in (*failed_leases, *unclaimed)},
        now=now,
    )
    if job.state not in _REASSIGNABLE:
        return False
    transition_job(
        session, job, JobState.REASSIGNED, message=message, extra=extra, now=now
    )
    job.scheduled_node_id = None
    job.rendezvous_node_id = None
    return True


async def _node_names(
    session: AsyncSession, node_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Real node names for a set of ids, so timeline messages can name the node
    that did not pick up its work instead of printing a UUID."""
    if not node_ids:
        return {}
    rows = (
        await session.execute(select(Node.id, Node.name).where(Node.id.in_(node_ids)))
    ).all()
    return {node_id: name for node_id, name in rows}


def _overdue_message(
    *,
    timed_out: list[Lease],
    unclaimed: list[Lease],
    names: dict[uuid.UUID, str],
    ttl_seconds: int,
) -> str:
    """The job-timeline sentence for one swept attempt.

    Two genuinely different events, so two different sentences — a reader of the
    timeline must be able to tell "the node dropped work it had taken" from "the
    node never picked the work up", because only the first is that node's fault.
    """
    parts: list[str] = []
    if timed_out:
        parts.append(
            f"{len(timed_out)} cohort lease(s) expired without progress; "
            "the whole attempt was torn down and requeued for "
            "rescheduling under a new lease epoch."
        )
    if unclaimed:
        who = ", ".join(
            sorted({names.get(lease.node_id, str(lease.node_id)) for lease in unclaimed})
        )
        parts.append(
            f"{who} did not pick up the work in time (the offer went unclaimed "
            f"for its {ttl_seconds}s lease TTL); rescheduling. No reliability "
            "penalty: the node never took this work on."
        )
    return " ".join(parts)


async def sweep_expired_leases(
    session: AsyncSession, *, settings: Settings
) -> int:
    """Sweep overdue cohort leases and reassign the whole attempt. Returns the
    number of leases swept. Caller commits.

    "Overdue" covers two events that look alike and mean opposite things — the
    distinction is the point of this function (ADR-003 addendum):

    * an **ACTIVE** lease past its TTL: a rank that took the work on and stopped
      renewing → ``EXPIRED`` with that node's ``lease_failure_count`` += 1. A
      node that stops making progress on work it holds is a real reliability
      signal (ADR-009).
    * a **PENDING** slot past its TTL: a rank that never claimed → ``UNCLAIMED``
      with **no** reliability effect. The scheduler offered work that was not
      picked up; that can be the node's fault (a dead agent) or the system's (an
      agent still busy with earlier work, or a claim-poll/TTL race), and we
      cannot tell which from this event alone — so it earns no penalty. A
      genuinely dead node is caught by the φ-accrual detector (ADR-004), which
      declares it OFFLINE from its *own* recorded evidence (missed heartbeats)
      and penalises it there.

    The rest of that attempt's cohort is then torn down (siblings RELEASED, no
    reliability hit — the drop was not their fault) and the job → REASSIGNED with
    its cohort/rendezvous wiring cleared, so a later pass re-selects N nodes
    under a fresh epoch (ADR-005: one rank dropping fails the whole attempt in
    M5; in-flight single-rank replacement is M6). Locked ``SKIP LOCKED`` so a
    concurrent sweep never double-processes.
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

    names = await _node_names(session, [lease.node_id for lease in overdue])
    swept_count = 0
    timed_out_count = 0
    unclaimed_count = 0
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
        # Defensive: clear any leftover stale-epoch overdue rows (superseded
        # attempts) — a cleanup only, no reliability hit, no reassignment. Each
        # row still lands in the terminal state that describes what really
        # happened to it, so the reliability inputs stay honest.
        stale_overdue = [
            lease for lease in overdue_leases
            if lease.lease_epoch != job.current_lease_epoch
        ]
        for lease in stale_overdue:
            if lease.state is LeaseState.PENDING:
                lease.state = LeaseState.UNCLAIMED
                unclaimed_count += 1
            else:
                lease.state = LeaseState.EXPIRED
                timed_out_count += 1
            swept_count += 1

        if not current_overdue:
            continue
        swept_count += len(current_overdue)
        # THE distinction (see this function's docstring): a lease that was
        # ACTIVE when it lapsed is the node's failure; a slot still PENDING when
        # it lapsed was never picked up and is nobody's failure.
        timed_out = [
            lease for lease in current_overdue if lease.state is LeaseState.ACTIVE
        ]
        unclaimed = [
            lease for lease in current_overdue if lease.state is LeaseState.PENDING
        ]
        timed_out_count += len(timed_out)
        unclaimed_count += len(unclaimed)
        # Reuse the one shared reassignment code path (also used by the M6
        # φ-accrual detector): expire the timed-out leases with a reliability
        # hit, mark the unclaimed offers UNCLAIMED without one, release the rest
        # of the cohort, and requeue the job under a fresh epoch.
        await reassign_job_attempt(
            session,
            job=job,
            failed_leases=timed_out,
            unclaimed_leases=unclaimed,
            unclaimed_backoff_seconds=settings.unclaimed_offer_backoff_seconds,
            now=now,
            message=_overdue_message(
                timed_out=timed_out,
                unclaimed=unclaimed,
                names=names,
                ttl_seconds=settings.lease_ttl_seconds,
            ),
            extra={
                "expired_ranks": sorted(lease.rank for lease in timed_out),
                "unclaimed_ranks": sorted(lease.rank for lease in unclaimed),
                "unclaimed_nodes": sorted(
                    names.get(lease.node_id, str(lease.node_id)) for lease in unclaimed
                ),
                "lease_epoch": job.current_lease_epoch,
                "trigger": "lease-ttl-sweep",
            },
        )

    await session.flush()
    # Two counters for two events: an expiry is a node that dropped work it
    # held; an unclaimed offer is not, and conflating them in
    # orchestrator_lease_expiries_total made a busy agent look like a flapping
    # one on the dashboard.
    if timed_out_count:
        lease_expiries_total.inc(timed_out_count)
    if unclaimed_count:
        lease_offers_unclaimed_total.inc(unclaimed_count)
    return swept_count
