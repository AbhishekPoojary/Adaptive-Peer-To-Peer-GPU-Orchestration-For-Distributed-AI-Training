"""RoundRobin scheduler (ADR-009 baseline).

Fairness is derived from persisted rotation state: ``Node.last_assigned_at``,
stamped by the scheduling service each time this strategy places a job on a
node. The next pick is the eligible node whose last assignment is oldest, with
a node that has never been assigned (``NULL``) treated as infinitely old so it
goes first. Ties break deterministically on node name.

Deriving from a timestamp rather than an in-memory counter means rotation
survives an orchestrator restart and stays correct as the eligible set changes
between passes.
"""

from __future__ import annotations

from datetime import datetime

from orchestrator.models.job import Job
from orchestrator.schedulers.base import NodeSnapshot

# A node never assigned sorts before any real timestamp.
_EPOCH = datetime.min


class RoundRobinScheduler:
    """Cycle fairly over eligible nodes by oldest last-assignment time."""

    name = "round_robin"

    async def select_node(
        self, job: Job, candidates: list[NodeSnapshot]
    ) -> NodeSnapshot | None:
        if not candidates:
            return None

        def sort_key(s: NodeSnapshot) -> tuple[datetime, str]:
            last = s.node.last_assigned_at
            # Normalise to naive UTC-comparable: all stored timestamps are tz-aware
            # from Postgres; the never-assigned sentinel is tz-naive datetime.min,
            # so compare on POSIX-like ordinal via replace(tzinfo=None) fallback.
            if last is None:
                return (_EPOCH, s.node.name)
            return (last.replace(tzinfo=None), s.node.name)

        return min(candidates, key=sort_key)
