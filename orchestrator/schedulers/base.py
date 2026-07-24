"""Scheduler interface, node snapshots, and the shared hard-filter stage
(ADR-009).

Placement is two stages. First the *hard filters* (this module) reduce the
fleet to nodes that could physically and safely run the job — every candidate a
scheduler sees has already passed them. Then a ``Scheduler`` strategy ranks the
survivors and picks one (or none).

Everything here is pure over ``NodeSnapshot`` values: no DB, no wall-clock reads
except the explicit ``now`` passed in. That keeps the filters and strategies
unit-testable without a database and keeps ADR-009's "hard filters run before
any scheduler sees candidates" a structural guarantee, not a convention.

No load or reliability value is ever fabricated: a node with no telemetry
sample yet has ``current_load(...) is None`` (unknown), which strategies treat
as least-preferred rather than assumed-idle (CONTRIBUTING.md rules 1-3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from orchestrator.models.job import Job
from orchestrator.models.node import Node, NodeStatus, NodeTelemetrySample


@dataclass(frozen=True)
class NodeSnapshot:
    """A node plus the inputs a scheduler needs to rank it.

    ``latest_sample`` is the node's most recent telemetry reading, or ``None``
    if it has never reported one. ``has_active_lease`` is ``True`` when the node
    is already occupied — either holding an ACTIVE lease or the pending target
    of a not-yet-claimed SCHEDULED job — enforcing "one job per node" (M2).
    """

    node: Node
    latest_sample: NodeTelemetrySample | None
    has_active_lease: bool


@runtime_checkable
class Scheduler(Protocol):
    """A placement strategy. Ranks pre-filtered candidates and picks one (or N).

    ``rank_candidates`` receives only candidates that already pass every hard
    filter, so a strategy never needs to re-check eligibility — it only orders
    them best-first. ``select_node`` is the world_size=1 convenience wrapper
    (the best candidate, or ``None`` when the pool is empty); a world_size=N
    cohort takes the first N of ``rank_candidates`` (see
    ``services.scheduling``). Baselines generalise to top-N by construction:
    the same ordering that picks one node picks the best N.
    """

    name: str

    def rank_candidates(
        self, job: Job, candidates: list[NodeSnapshot]
    ) -> list[NodeSnapshot]: ...

    async def select_node(
        self, job: Job, candidates: list[NodeSnapshot]
    ) -> NodeSnapshot | None: ...


def max_gpu_vram_bytes(node: Node) -> int | None:
    """Largest single-GPU VRAM (bytes) from the node's real inventory.

    ``None`` when the node reported no GPUs — a CPU-only node. Reads the
    truthful hardware inventory captured at enrollment; nothing is invented.
    """
    gpus = node.hardware.get("gpus") or []
    vrams = [int(g["vram_bytes"]) for g in gpus if g.get("vram_bytes") is not None]
    return max(vrams) if vrams else None


def current_load(snapshot: NodeSnapshot) -> float | None:
    """Real current load in ``[0, 100]`` from the latest telemetry sample.

    GPU utilisation (max across the node's GPUs) when GPU telemetry is present,
    else CPU percent. ``None`` when there is no sample yet — genuinely unknown,
    never coerced to 0. Callers must treat ``None`` as least-preferred, not idle.
    """
    sample = snapshot.latest_sample
    if sample is None:
        return None
    if sample.gpu:
        utils = [float(g["util_percent"]) for g in sample.gpu]
        if utils:
            return max(utils)
    return float(sample.cpu_percent)


def _is_stale(node: Node, *, now: datetime, stale_seconds: float) -> bool:
    if node.last_heartbeat_at is None:
        return True
    return (now - node.last_heartbeat_at) > timedelta(seconds=stale_seconds)


def passes_hard_filters(
    job: Job, snapshot: NodeSnapshot, *, now: datetime, stale_seconds: float
) -> bool:
    """True iff ``snapshot`` may run ``job`` (ADR-009 hard filters).

    A node qualifies only when it is ONLINE, not DRAINING, heartbeating within
    the staleness window, free of an active lease, and — for a GPU job — has a
    GPU large enough. A CPU job (``min_gpu_mem_bytes is None``) accepts any node
    that clears the other filters; a GPU job requires a big-enough GPU, so a
    CPU-only node is filtered out.
    """
    node = snapshot.node
    # ONLINE is the only runnable status: this rejects both OFFLINE and DRAINING.
    if node.status is not NodeStatus.ONLINE:
        return False
    if _is_stale(node, now=now, stale_seconds=stale_seconds):
        return False
    if snapshot.has_active_lease:
        return False

    min_gpu = job.spec.get("min_gpu_mem_bytes")
    if min_gpu is not None:
        node_vram = max_gpu_vram_bytes(node)
        if node_vram is None or node_vram < int(min_gpu):
            return False
    return True


def eligible_candidates(
    job: Job,
    snapshots: list[NodeSnapshot],
    *,
    now: datetime,
    stale_seconds: float,
) -> list[NodeSnapshot]:
    """The subset of ``snapshots`` that passes every hard filter for ``job``."""
    return [
        s
        for s in snapshots
        if passes_hard_filters(job, s, now=now, stale_seconds=stale_seconds)
    ]
