"""Scheduling service: turn QUEUED/REASSIGNED jobs into placements (ADR-009).

A scheduler *pass* loads jobs awaiting placement, builds a real snapshot of the
fleet (node row + latest telemetry + whether the node is already occupied),
runs the shared hard filters, and forms each job's training cohort. A placed
job moves QUEUED/REASSIGNED → SCHEDULED with N PENDING leases (one per rank)
recorded at a freshly minted attempt epoch; each rank's agent later claims its
lease (``services.leases.claim_job_for_node``), flipping it ACTIVE.

Cohort formation (M5, ADR-005) is one path for every strategy: the job's
scheduler ranks the eligible pool and the top ``world_size`` nodes are taken
(each baseline generalises to top-N by construction; ``adaptive`` scores by
S_i, takes the N lowest, and audit-logs the decision with the whole cohort
marked selected — reliability is DB-backed so that work lives here in
:func:`place_job_cohort`). The rendezvous host is the highest-reliability
member (Wilson lower bound, ADR-005) and takes rank 0. A world_size=1 job is a
one-member cohort, so the M2/M3 single-rank behaviour is exactly the N=1 case.

"One job per node" is enforced structurally: a node counts as occupied if it
holds any non-terminal (PENDING or ACTIVE) lease, and additionally cannot be
picked twice within a single pass.

Passes must not run concurrently in-process (two passes could pick the same
free node for two jobs); the background runner serialises them under a lock.
Within a pass, jobs are locked ``FOR UPDATE SKIP LOCKED`` so an overlapping
claim/cancel never double-processes the same job. The service flushes; the
caller commits.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from orchestrator.core.config import Settings
from orchestrator.core.metrics import jobs_placed_total, scheduling_decisions_total
from orchestrator.models.job import Job, JobState
from orchestrator.models.lease import Lease, LeaseState
from orchestrator.models.node import Node, NodeTelemetrySample
from orchestrator.models.scheduling import (
    SchedulingDecision,
    SchedulingDecisionCandidate,
)
from orchestrator.schedulers.adaptive import (
    AdaptiveScheduler,
    CandidateReliability,
    ScoredCandidate,
    score_candidates,
    select_top_n,
)
from orchestrator.schedulers.base import NodeSnapshot, eligible_candidates
from orchestrator.schedulers.registry import get_scheduler
from orchestrator.schedulers.reliability import decay_weight, wilson_lower_bound
from orchestrator.schemas.scheduling import (
    SchedulingCandidateOut,
    SchedulingDecisionOut,
)
from orchestrator.services.jobs import transition_job

logger = logging.getLogger("orchestrator.scheduling")

# Job states that are awaiting (re)placement by a scheduler pass.
_PLACEABLE = (JobState.QUEUED, JobState.REASSIGNED)

# Lease outcomes that count toward reliability. COMPLETED is a success; FAILED
# (the node reported a real failure) and EXPIRED (the node held an ACTIVE lease
# and stopped making progress) are failures.
#
# Two terminal states are deliberately excluded because neither is evidence
# about the node: RELEASED (a cancellation or a cohort sibling's failure, per
# services.jobs.cancel_job) and UNCLAIMED (an offered PENDING slot that was
# never picked up — nobody took that work on, so nobody dropped it; see
# services.leases.sweep_expired_leases and docs/adr/ADR-003-addendum.md).
# This is the authoritative R_i input: the flat Node counters are a display
# metric, so excluding UNCLAIMED *here* is what actually keeps the adaptive
# scheduler's reliability term honest.
_SUCCESS_STATES = (LeaseState.COMPLETED,)
_FAILURE_STATES = (LeaseState.FAILED, LeaseState.EXPIRED)
_RELIABILITY_STATES = (*_SUCCESS_STATES, *_FAILURE_STATES)


async def _busy_node_ids(session: AsyncSession) -> set[object]:
    """Nodes that are occupied and so ineligible ("one job per node").

    A node is busy if it holds any non-terminal lease — ACTIVE (running a rank)
    or PENDING (a cohort slot the scheduler created that the node has not yet
    claimed). The PENDING case is how a multi-rank cohort reserves all N of its
    nodes at schedule time, before any rank has claimed."""
    busy = (
        await session.execute(
            select(Lease.node_id)
            .where(Lease.state.in_((LeaseState.ACTIVE, LeaseState.PENDING)))
            .distinct()
        )
    ).scalars().all()
    return set(busy)


async def _node_snapshots(session: AsyncSession) -> list[NodeSnapshot]:
    """Every node with its latest telemetry sample and occupied flag.

    Latest-sample-per-node comes from a single ``DISTINCT ON`` query (no N+1),
    mirroring ``services.nodes.list_nodes``.
    """
    latest_subq = (
        select(NodeTelemetrySample)
        .distinct(NodeTelemetrySample.node_id)
        .order_by(
            NodeTelemetrySample.node_id,
            NodeTelemetrySample.ts.desc(),
            NodeTelemetrySample.id.desc(),
        )
        .subquery()
    )
    latest_sample = aliased(NodeTelemetrySample, latest_subq)
    stmt = (
        select(Node, latest_sample)
        .outerjoin(latest_sample, latest_subq.c.node_id == Node.id)
        .order_by(Node.name)
        .execution_options(populate_existing=True)
    )
    rows = (await session.execute(stmt)).all()

    busy = await _busy_node_ids(session)
    return [
        NodeSnapshot(node=node, latest_sample=sample, has_active_lease=node.id in busy)
        for node, sample in rows
    ]


async def _reliability_inputs(
    session: AsyncSession,
    candidates: list[NodeSnapshot],
    *,
    now: datetime,
    settings: Settings,
) -> list[CandidateReliability]:
    """Build decay-weighted success/failure pseudo-counts for each candidate.

    One query pulls every reliability-relevant terminal lease for the candidate
    nodes (no N+1). Each outcome is weighted by ``decay_weight(age)`` where age
    is measured from ``COALESCE(released_at, expires_at)`` — the real moment the
    outcome occurred (``released_at`` for COMPLETED/FAILED, ``expires_at`` for a
    swept EXPIRED lease that carries no release time). Weighted successes and
    failures are added on top of the node's declared Beta prior
    (``reliability_prior_alpha``/``beta``), so a node with no history carries
    only its prior — never a fabricated "assume reliable" default.

    The flat ``Node.lease_success_count``/``lease_failure_count`` counters are
    intentionally NOT used here: they cannot carry the per-outcome timestamps
    time decay needs. They are kept as a cheap denormalized display metric (the
    M1 read API) and a sanity cross-check, but reliability scoring reads real
    ``Lease`` rows.
    """
    half_life = settings.reliability_decay_halflife_seconds
    node_ids = [s.node.id for s in candidates]

    # weighted[node_id] = [weighted_success, weighted_failure], seeded with the
    # node's declared prior pseudo-counts (undecayed).
    weighted: dict[object, list[float]] = {
        s.node.id: [s.node.reliability_prior_alpha, s.node.reliability_prior_beta]
        for s in candidates
    }

    if node_ids:
        outcome_ts = func.coalesce(Lease.released_at, Lease.expires_at)
        rows = (
            await session.execute(
                select(Lease.node_id, Lease.state, outcome_ts).where(
                    Lease.node_id.in_(node_ids),
                    Lease.state.in_(_RELIABILITY_STATES),
                )
            )
        ).all()
        for node_id, state, ts in rows:
            age_seconds = (now - ts).total_seconds()
            weight = decay_weight(age_seconds, half_life)
            if state in _SUCCESS_STATES:
                weighted[node_id][0] += weight
            else:
                weighted[node_id][1] += weight

    return [
        CandidateReliability(
            snapshot=s,
            weighted_success=weighted[s.node.id][0],
            weighted_failure=weighted[s.node.id][1],
        )
        for s in candidates
    ]


def _persist_decision(
    session: AsyncSession,
    *,
    job: Job,
    scored: list[ScoredCandidate],
    selected_ids: set[uuid.UUID],
    rendezvous_id: uuid.UUID | None,
    settings: Settings,
) -> None:
    """Write the SchedulingDecision + one candidate row per considered node.

    ``selected_ids`` is the whole chosen cohort (one id for a world_size=1 job,
    N for a cohort); ``was_selected`` is stamped ``True`` for each. The
    decision's ``selected_node_id`` is the rendezvous host (rank 0) — the single
    node the audit points at as the attempt's coordinator (ADR-005).
    """
    decision = SchedulingDecision(
        job_id=job.id,
        scheduler_name=AdaptiveScheduler.name,
        alpha=settings.scheduler_alpha_load,
        beta=settings.scheduler_beta_reliability,
        gamma=settings.scheduler_gamma_latency,
        reliability_halflife_seconds=settings.reliability_decay_halflife_seconds,
        wilson_z=settings.reliability_wilson_z,
        selected_node_id=rendezvous_id,
    )
    session.add(decision)
    scheduling_decisions_total.inc()
    for cand in scored:
        session.add(
            SchedulingDecisionCandidate(
                decision=decision,
                node_id=cand.snapshot.node.id,
                node_name=cand.snapshot.node.name,
                l_score=cand.l_score,
                r_score=cand.r_score,
                d_score=cand.d_score,
                s_score=cand.s_score,
                raw_util=cand.raw_util,
                raw_rtt_ewma_ms=cand.raw_rtt_ewma_ms,
                weighted_success=cand.weighted_success,
                weighted_failure=cand.weighted_failure,
                was_selected=cand.snapshot.node.id in selected_ids,
            )
        )


@dataclass(frozen=True)
class CohortMember:
    """One node's assigned slot in a scheduled training cohort (ADR-005).

    ``rank`` is the 0..world_size-1 slot; ``rank == 0`` is always the rendezvous
    host (``is_rendezvous_host`` True), the highest-reliability member.
    """

    rank: int
    snapshot: NodeSnapshot
    is_rendezvous_host: bool


def _wilson_by_node(
    reliability: list[CandidateReliability], *, wilson_z: float
) -> dict[uuid.UUID, float]:
    """Wilson lower-bound reliability (ADR-009's R term) per candidate node, from
    its decay-weighted success/failure pseudo-counts. Reused to pick the
    rendezvous host regardless of which scheduler formed the cohort (ADR-005)."""
    return {
        ci.snapshot.node.id: wilson_lower_bound(
            ci.weighted_success, ci.weighted_failure, wilson_z
        )
        for ci in reliability
    }


async def place_job_cohort(
    session: AsyncSession,
    job: Job,
    candidates: list[NodeSnapshot],
    *,
    world_size: int,
    now: datetime,
    settings: Settings,
) -> list[CohortMember] | None:
    """Form and persist a world_size=N training cohort for ``job`` (ADR-005).

    ``candidates`` are already hard-filtered. Returns ``None`` (job left for a
    later pass, no partial placement) when fewer than ``world_size`` nodes are
    eligible — a distributed job needs its *whole* cohort or nothing.

    Otherwise: the job's scheduler ranks the pool and the top ``world_size``
    nodes are taken (each baseline generalises to top-N by construction; the
    adaptive scorer takes the N lowest ``S_i`` and audits the decision with the
    whole cohort marked selected). The rendezvous host is then the
    highest-reliability member (Wilson lower bound, ADR-005) and is assigned
    rank 0; the rest keep scheduler order at ranks 1..N-1. A single attempt
    epoch is minted once and stamped on all N PENDING leases — the ADR-003
    fence now guards a whole cohort. Each rank later claims its PENDING lease
    (``services.leases.claim_job_for_node``), flipping it ACTIVE.
    """
    if len(candidates) < world_size:
        return None

    reliability = await _reliability_inputs(
        session, candidates, now=now, settings=settings
    )
    wilson = _wilson_by_node(reliability, wilson_z=settings.reliability_wilson_z)

    scored: list[ScoredCandidate] = []
    if job.scheduler_name == AdaptiveScheduler.name:
        scored = score_candidates(
            reliability,
            alpha=settings.scheduler_alpha_load,
            beta=settings.scheduler_beta_reliability,
            gamma=settings.scheduler_gamma_latency,
            wilson_z=settings.reliability_wilson_z,
            load_significant_spread=settings.scheduler_load_significant_spread,
            latency_significant_spread_ms=settings.scheduler_latency_significant_spread_ms,
        )
        selected = [sc.snapshot for sc in select_top_n(scored, world_size)]
    else:
        scheduler = get_scheduler(job.scheduler_name)
        selected = scheduler.rank_candidates(job, candidates)[:world_size]

    # Rendezvous host: the highest-reliability cohort member (ADR-005). Ties
    # break toward the node the scheduler itself ranked higher (smaller index).
    rdzv_idx = max(
        range(len(selected)),
        key=lambda i: (wilson[selected[i].node.id], -i),
    )
    rdzv = selected[rdzv_idx]
    members = [CohortMember(rank=0, snapshot=rdzv, is_rendezvous_host=True)]
    others = [s for i, s in enumerate(selected) if i != rdzv_idx]
    members += [
        CohortMember(rank=r, snapshot=s, is_rendezvous_host=False)
        for r, s in enumerate(others, start=1)
    ]

    # Mint the attempt epoch ONCE; every rank's lease shares it (ADR-003 cohort
    # fence). A fresh attempt after any failure increments it again.
    new_epoch = job.current_lease_epoch + 1
    job.current_lease_epoch = new_epoch
    ttl = timedelta(seconds=settings.lease_ttl_seconds)
    for member in members:
        session.add(
            Lease(
                id=uuid.uuid4(),
                job_id=job.id,
                node_id=member.snapshot.node.id,
                lease_epoch=new_epoch,
                rank=member.rank,
                state=LeaseState.PENDING,
                granted_at=now,
                # A PENDING slot not claimed within the TTL is swept and the
                # attempt reassigned — but it ends UNCLAIMED, not EXPIRED: an
                # offer nobody picked up is not a node failure (ADR-003
                # addendum, services.leases.sweep_expired_leases).
                expires_at=now + ttl,
            )
        )
        member.snapshot.node.last_assigned_at = now
    job.rendezvous_node_id = rdzv.node.id
    job.scheduled_node_id = rdzv.node.id  # rank-0 node, for read continuity

    if job.scheduler_name == AdaptiveScheduler.name:
        _persist_decision(
            session,
            job=job,
            scored=scored,
            selected_ids={m.snapshot.node.id for m in members},
            rendezvous_id=rdzv.node.id,
            settings=settings,
        )
        # ADR-009: the weights are logged with every decision, not applied silently.
        logger.info(
            "adaptive cohort job=%s world_size=%d alpha=%.3f beta=%.3f gamma=%.3f "
            "halflife_s=%.1f wilson_z=%.3f candidates=%d cohort=%s rendezvous=%s",
            job.id,
            world_size,
            settings.scheduler_alpha_load,
            settings.scheduler_beta_reliability,
            settings.scheduler_gamma_latency,
            settings.reliability_decay_halflife_seconds,
            settings.reliability_wilson_z,
            len(scored),
            [m.snapshot.node.name for m in members],
            rdzv.node.name,
        )

    cohort_desc = ", ".join(
        f"rank {m.rank}={m.snapshot.node.name}"
        + ("(rendezvous)" if m.is_rendezvous_host else "")
        for m in members
    )
    transition_job(
        session,
        job,
        JobState.SCHEDULED,
        message=(
            f"Scheduled by the {job.scheduler_name} scheduler as a world_size="
            f"{world_size} cohort (epoch {new_epoch}): {cohort_desc}. "
            f"Rendezvous host: {rdzv.node.name}."
        ),
        extra={
            "world_size": world_size,
            "lease_epoch": new_epoch,
            "rendezvous_node_id": str(rdzv.node.id),
            "rendezvous_node_name": rdzv.node.name,
            "cohort": [
                {
                    "rank": m.rank,
                    "node_id": str(m.snapshot.node.id),
                    "node_name": m.snapshot.node.name,
                    "is_rendezvous_host": m.is_rendezvous_host,
                }
                for m in members
            ],
        },
        now=now,
    )
    jobs_placed_total.inc()
    return members


async def run_scheduler_pass(session: AsyncSession, *, settings: Settings) -> int:
    """Place as many awaiting jobs as there are eligible nodes. Returns the
    number placed. Caller commits.

    Jobs are considered oldest-submission-first and locked ``SKIP LOCKED``; a
    job with no eligible node is left as-is for the next pass (no fabricated
    placement).
    """
    now = datetime.now(UTC)
    jobs = (
        await session.execute(
            select(Job)
            .where(Job.state.in_(_PLACEABLE))
            .order_by(Job.submitted_at)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    if not jobs:
        return 0

    snapshots = await _node_snapshots(session)
    assigned_this_pass: set[object] = set()
    placed = 0

    for job in jobs:
        # world_size=N needs N distinct nodes for one attempt; a partial cohort
        # is never placed (place_job_cohort returns None and the job waits).
        world_size = int(job.spec.get("world_size", 1) or 1)
        candidates = [
            s for s in snapshots if s.node.id not in assigned_this_pass
        ]
        elig = eligible_candidates(
            job, candidates, now=now, stale_seconds=settings.heartbeat_stale_seconds
        )
        members = await place_job_cohort(
            session, job, elig, world_size=world_size, now=now, settings=settings
        )
        if members is None:
            continue
        for member in members:
            assigned_this_pass.add(member.snapshot.node.id)
        placed += 1

    await session.flush()
    return placed


# --- Read view ---------------------------------------------------------------


async def list_scheduling_decisions(
    session: AsyncSession, *, job_id: uuid.UUID
) -> list[SchedulingDecisionOut]:
    """Every scheduling decision recorded for a job, newest first.

    Empty for jobs placed by a non-audited baseline (only ``adaptive`` writes
    decisions), or for a job never scheduled. Candidates within a decision are
    ordered by ``s_score`` (the winner — lowest — first).
    """
    decisions = (
        await session.execute(
            select(SchedulingDecision)
            .where(SchedulingDecision.job_id == job_id)
            .options(selectinload(SchedulingDecision.candidates))
            .order_by(SchedulingDecision.ts.desc(), SchedulingDecision.id.desc())
        )
    ).scalars().all()

    return [
        SchedulingDecisionOut(
            id=d.id,
            job_id=d.job_id,
            ts=d.ts,
            scheduler_name=d.scheduler_name,
            alpha=d.alpha,
            beta=d.beta,
            gamma=d.gamma,
            reliability_halflife_seconds=d.reliability_halflife_seconds,
            wilson_z=d.wilson_z,
            selected_node_id=d.selected_node_id,
            candidates=[
                SchedulingCandidateOut(
                    node_id=c.node_id,
                    node_name=c.node_name,
                    l_score=c.l_score,
                    r_score=c.r_score,
                    d_score=c.d_score,
                    s_score=c.s_score,
                    raw_util=c.raw_util,
                    raw_rtt_ewma_ms=c.raw_rtt_ewma_ms,
                    weighted_success=c.weighted_success,
                    weighted_failure=c.weighted_failure,
                    was_selected=c.was_selected,
                )
                for c in d.candidates
            ],
        )
        for d in decisions
    ]
