"""Scheduling service: turn QUEUED/REASSIGNED jobs into placements (ADR-009).

A scheduler *pass* loads jobs awaiting placement, builds a real snapshot of the
fleet (node row + latest telemetry + whether the node is already occupied),
runs the shared hard filters, and lets each job's chosen strategy pick a node.
A placed job moves QUEUED/REASSIGNED → SCHEDULED with its target recorded; the
agent later claims it (``services.leases.claim_job_for_node``).

The ``adaptive`` strategy (M3) is special: ranking a survivor needs its real
lease history (reliability) and every decision is audit-logged. That work is
DB-backed, so it lives here in :func:`place_job_adaptive` rather than in the
pure ``select_node`` contract the baselines share; the pass routes ``adaptive``
jobs to it by name. The baselines still go through ``scheduler.select_node``.

"One job per node" is enforced structurally: a node counts as occupied if it
holds an ACTIVE lease *or* is already the pending target of a SCHEDULED job,
and additionally cannot be picked twice within a single pass.

Passes must not run concurrently in-process (two passes could pick the same
free node for two jobs); the background runner serialises them under a lock.
Within a pass, jobs are locked ``FOR UPDATE SKIP LOCKED`` so an overlapping
claim/cancel never double-processes the same job. The service flushes; the
caller commits.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from orchestrator.core.config import Settings
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
    select_best,
)
from orchestrator.schedulers.base import NodeSnapshot, eligible_candidates
from orchestrator.schedulers.registry import get_scheduler
from orchestrator.schedulers.reliability import decay_weight
from orchestrator.schemas.scheduling import (
    SchedulingCandidateOut,
    SchedulingDecisionOut,
)
from orchestrator.services.jobs import transition_job

logger = logging.getLogger("orchestrator.scheduling")

# Job states that are awaiting (re)placement by a scheduler pass.
_PLACEABLE = (JobState.QUEUED, JobState.REASSIGNED)

# Lease outcomes that count toward reliability. COMPLETED is a success;
# FAILED and EXPIRED are failures. RELEASED (a cancellation) is deliberately
# excluded — it is not a node's fault, matching services.jobs.cancel_job.
_SUCCESS_STATES = (LeaseState.COMPLETED,)
_FAILURE_STATES = (LeaseState.FAILED, LeaseState.EXPIRED)
_RELIABILITY_STATES = (*_SUCCESS_STATES, *_FAILURE_STATES)


async def _busy_node_ids(session: AsyncSession) -> set[object]:
    """Nodes that are occupied: holding an ACTIVE lease or the pending target
    of a SCHEDULED (not-yet-claimed) job. Such nodes are ineligible ("one job
    per node")."""
    active = (
        await session.execute(
            select(Lease.node_id).where(Lease.state == LeaseState.ACTIVE).distinct()
        )
    ).scalars().all()
    scheduled = (
        await session.execute(
            select(Job.scheduled_node_id)
            .where(
                Job.state == JobState.SCHEDULED, Job.scheduled_node_id.is_not(None)
            )
            .distinct()
        )
    ).scalars().all()
    return {*active, *scheduled}


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
    winner: ScoredCandidate | None,
    settings: Settings,
) -> None:
    """Write the SchedulingDecision + one candidate row per considered node."""
    decision = SchedulingDecision(
        job_id=job.id,
        scheduler_name=AdaptiveScheduler.name,
        alpha=settings.scheduler_alpha_load,
        beta=settings.scheduler_beta_reliability,
        gamma=settings.scheduler_gamma_latency,
        reliability_halflife_seconds=settings.reliability_decay_halflife_seconds,
        wilson_z=settings.reliability_wilson_z,
        selected_node_id=winner.snapshot.node.id if winner is not None else None,
    )
    session.add(decision)
    winner_node_id = winner.snapshot.node.id if winner is not None else None
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
                was_selected=cand.snapshot.node.id == winner_node_id,
            )
        )


async def place_job_adaptive(
    session: AsyncSession,
    job: Job,
    candidates: list[NodeSnapshot],
    *,
    now: datetime,
    settings: Settings,
) -> NodeSnapshot | None:
    """Score ``candidates`` for ``job`` by S_i, persist the audit trail, and
    return the winner's snapshot (or ``None`` if there was nothing to rank).

    ``candidates`` are already hard-filtered by the caller. With an empty pool
    no decision is recorded (there is nothing to explain and no placement to
    make). Otherwise every candidate is scored and one ``SchedulingDecision``
    with per-candidate rows is written before the winner is returned.
    """
    if not candidates:
        return None

    reliability = await _reliability_inputs(
        session, candidates, now=now, settings=settings
    )
    scored = score_candidates(
        reliability,
        alpha=settings.scheduler_alpha_load,
        beta=settings.scheduler_beta_reliability,
        gamma=settings.scheduler_gamma_latency,
        wilson_z=settings.reliability_wilson_z,
    )
    winner = select_best(scored)
    _persist_decision(
        session, job=job, scored=scored, winner=winner, settings=settings
    )

    # ADR-009: the weights are logged with every decision, not applied silently.
    logger.info(
        "adaptive decision job=%s alpha=%.3f beta=%.3f gamma=%.3f "
        "halflife_s=%.1f wilson_z=%.3f candidates=%d winner=%s (S=%s)",
        job.id,
        settings.scheduler_alpha_load,
        settings.scheduler_beta_reliability,
        settings.scheduler_gamma_latency,
        settings.reliability_decay_halflife_seconds,
        settings.reliability_wilson_z,
        len(scored),
        winner.snapshot.node.name if winner is not None else None,
        f"{winner.s_score:.4f}" if winner is not None else None,
    )
    return winner.snapshot if winner is not None else None


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
        candidates = [
            s for s in snapshots if s.node.id not in assigned_this_pass
        ]
        elig = eligible_candidates(
            job, candidates, now=now, stale_seconds=settings.heartbeat_stale_seconds
        )
        scheduler = get_scheduler(job.scheduler_name)
        if job.scheduler_name == AdaptiveScheduler.name:
            chosen = await place_job_adaptive(
                session, job, elig, now=now, settings=settings
            )
        else:
            chosen = await scheduler.select_node(job, elig)
        if chosen is None:
            continue

        target = chosen.node
        job.scheduled_node_id = target.id
        target.last_assigned_at = now
        assigned_this_pass.add(target.id)
        transition_job(
            session,
            job,
            JobState.SCHEDULED,
            message=(
                f"Scheduled to node {target.name} by the {job.scheduler_name} scheduler."
            ),
            extra={"node_id": str(target.id), "node_name": target.name},
            now=now,
        )
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
