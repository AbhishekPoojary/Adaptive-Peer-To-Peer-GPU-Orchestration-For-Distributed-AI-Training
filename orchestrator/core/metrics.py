"""Prometheus instrumentation (M7).

Two kinds of series live here, and both are real:

* **Counters** advance exactly once at the real call site where the event they
  name actually happens (a heartbeat recorded, a scheduling decision persisted,
  a failure declared, a lease expired) — imported into the service modules
  that own those events. They reset to 0 on process restart, which is normal
  Prometheus counter behaviour, not fabrication.
* **Gauges** are recomputed from a real `SELECT ... GROUP BY` against Postgres
  immediately before every `/metrics` scrape (:func:`update_db_gauges`), so
  they always reflect the database's current truth rather than a value cached
  from whenever the process started.

Nothing here is hardcoded or computed from nothing (CONTRIBUTING.md): every
number is either a monotonic count of a real event or a live aggregate read
straight from the same tables the REST read API serves from.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.models.job import Job, JobState
from orchestrator.models.lease import Lease, LeaseState
from orchestrator.models.node import Node, NodeStatus

#: Dedicated registry (not the global default) so re-importing this module in
#: tests that build fresh FastAPI apps never risks a duplicate-registration
#: error — the module itself is only ever imported once per process, and the
#: registry is a plain module-level singleton.
REGISTRY = CollectorRegistry()

# --- Counters: incremented at the real call site the event happens --------

heartbeats_received_total = Counter(
    "orchestrator_heartbeats_received_total",
    "Total node heartbeats accepted by POST /nodes/{id}/heartbeat.",
    registry=REGISTRY,
)

scheduling_decisions_total = Counter(
    "orchestrator_scheduling_decisions_total",
    "Total adaptive-scheduler decisions persisted (ADR-009 audit trail). "
    "Only the 'adaptive' strategy records an audited decision per placement; "
    "round_robin/least_loaded placements are not counted here.",
    registry=REGISTRY,
)

jobs_placed_total = Counter(
    "orchestrator_jobs_placed_total",
    "Total jobs placed into a training cohort by any scheduler strategy.",
    registry=REGISTRY,
)

failure_detections_total = Counter(
    "orchestrator_failure_detections_total",
    "Total nodes declared OFFLINE by the phi-accrual failure detector (ADR-004).",
    registry=REGISTRY,
)

lease_expiries_total = Counter(
    "orchestrator_lease_expiries_total",
    "Total leases expired by the TTL sweep (a rank that stopped renewing or "
    "never claimed in time).",
    registry=REGISTRY,
)

# --- Gauges: set from a real DB read immediately before every scrape -------

nodes_by_status = Gauge(
    "orchestrator_nodes",
    "Current node count by status, read live from the nodes table.",
    ["status"],
    registry=REGISTRY,
)

jobs_by_state = Gauge(
    "orchestrator_jobs",
    "Current job count by state, read live from the jobs table.",
    ["state"],
    registry=REGISTRY,
)

leases_active = Gauge(
    "orchestrator_leases_active",
    "Current count of ACTIVE leases, read live from the leases table.",
    registry=REGISTRY,
)


async def update_db_gauges(session: AsyncSession) -> None:
    """Refresh every DB-backed gauge from a live query. Call once per scrape.

    Every known enum value is zeroed first so a status/state with zero current
    rows still reports `0` (a real count) rather than silently vanishing from
    the exposition — the zero itself is a real aggregate, not a placeholder.
    """
    for status in NodeStatus:
        nodes_by_status.labels(status=status.value).set(0)
    node_rows = (
        await session.execute(
            select(Node.status, func.count()).group_by(Node.status)
        )
    ).all()
    for status, count in node_rows:
        nodes_by_status.labels(status=status.value).set(count)

    for state in JobState:
        jobs_by_state.labels(state=state.value).set(0)
    job_rows = (
        await session.execute(select(Job.state, func.count()).group_by(Job.state))
    ).all()
    for state, count in job_rows:
        jobs_by_state.labels(state=state.value).set(count)

    active_count = (
        await session.execute(
            select(func.count()).select_from(Lease).where(Lease.state == LeaseState.ACTIVE)
        )
    ).scalar_one()
    leases_active.set(active_count)
