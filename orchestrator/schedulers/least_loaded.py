"""LeastLoaded scheduler (ADR-009 baseline).

Picks the eligible node with the lowest *real* current load — GPU utilisation
when GPU telemetry is present, otherwise CPU percent (see
``base.current_load``). A node that has not reported a telemetry sample yet has
unknown load; it is deprioritised (placed after every node with a real reading)
rather than assumed idle, honouring the anti-fabrication rules — an unmeasured
node must never win by looking like a free 0%.
"""

from __future__ import annotations

from orchestrator.models.job import Job
from orchestrator.schedulers.base import NodeSnapshot, current_load


class LeastLoadedScheduler:
    """Choose the least-loaded eligible node from real telemetry."""

    name = "least_loaded"

    async def select_node(
        self, job: Job, candidates: list[NodeSnapshot]
    ) -> NodeSnapshot | None:
        if not candidates:
            return None

        def sort_key(s: NodeSnapshot) -> tuple[int, float, str]:
            load = current_load(s)
            # (has_no_telemetry, load, name): nodes with a real reading (0) sort
            # before nodes with unknown load (1); within each group, lower load
            # then name for determinism.
            if load is None:
                return (1, 0.0, s.node.name)
            return (0, load, s.node.name)

        return min(candidates, key=sort_key)
