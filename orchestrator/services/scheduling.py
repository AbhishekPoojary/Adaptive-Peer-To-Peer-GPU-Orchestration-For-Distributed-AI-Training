"""Scheduling service: turn QUEUED/REASSIGNED jobs into placements (ADR-009).

A scheduler *pass* loads jobs awaiting placement, builds a real snapshot of the
fleet (node row + latest telemetry + whether the node is already occupied),
runs the shared hard filters, and lets each job's chosen strategy pick a node.
A placed job moves QUEUED/REASSIGNED → SCHEDULED with its target recorded; the
agent later claims it (``services.leases.claim_job_for_node``).

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

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from orchestrator.core.config import Settings
from orchestrator.models.job import Job, JobState
from orchestrator.models.lease import Lease, LeaseState
from orchestrator.models.node import Node, NodeTelemetrySample
from orchestrator.schedulers.base import NodeSnapshot, eligible_candidates
from orchestrator.schedulers.registry import get_scheduler
from orchestrator.services.jobs import transition_job

# Job states that are awaiting (re)placement by a scheduler pass.
_PLACEABLE = (JobState.QUEUED, JobState.REASSIGNED)


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
