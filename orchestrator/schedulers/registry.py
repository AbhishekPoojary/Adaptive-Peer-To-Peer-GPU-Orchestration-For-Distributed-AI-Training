"""Scheduler registry keyed by ``scheduler_name`` (ADR-009).

Three strategies are available: the ``round_robin`` and ``least_loaded``
baselines (M2) and the ``adaptive`` penalty-score scheduler (M3). A job
requesting anything else is rejected at submit time by validating against
``registered_names()`` — there is no silent fallback to a default that would
mask an unknown strategy.

``adaptive`` is DB-backed and audited, so its real placement path lives in
``services.scheduling.place_job_cohort`` (the scheduler pass routes to it by
name). It registers here only so the name is validatable/selectable; its
``rank_candidates``/``select_node`` are deliberately raising guards (see
``AdaptiveScheduler``).
"""

from __future__ import annotations

from orchestrator.schedulers.adaptive import AdaptiveScheduler
from orchestrator.schedulers.base import Scheduler
from orchestrator.schedulers.least_loaded import LeastLoadedScheduler
from orchestrator.schedulers.round_robin import RoundRobinScheduler

_REGISTRY: dict[str, Scheduler] = {
    RoundRobinScheduler.name: RoundRobinScheduler(),
    LeastLoadedScheduler.name: LeastLoadedScheduler(),
    AdaptiveScheduler.name: AdaptiveScheduler(),
}


def registered_names() -> frozenset[str]:
    """Names of every scheduler available for a job to request."""
    return frozenset(_REGISTRY)


def is_registered(name: str) -> bool:
    """True iff ``name`` is a registered scheduler."""
    return name in _REGISTRY


def get_scheduler(name: str) -> Scheduler:
    """Return the scheduler for ``name``. Raises ``KeyError`` if unregistered."""
    return _REGISTRY[name]
