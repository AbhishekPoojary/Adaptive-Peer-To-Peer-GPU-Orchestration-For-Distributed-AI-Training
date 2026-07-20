"""Scheduler registry keyed by ``scheduler_name`` (ADR-009).

Only the baselines exist after M2: ``round_robin`` and ``least_loaded``. The
adaptive penalty-score scheduler arrives in M3 and registers here then — until
it does, a job requesting ``adaptive`` is rejected at submit time by validating
against ``registered_names()``. There is no silent fallback to a default that
would mask an unknown strategy.
"""

from __future__ import annotations

from orchestrator.schedulers.base import Scheduler
from orchestrator.schedulers.least_loaded import LeastLoadedScheduler
from orchestrator.schedulers.round_robin import RoundRobinScheduler

_REGISTRY: dict[str, Scheduler] = {
    RoundRobinScheduler.name: RoundRobinScheduler(),
    LeastLoadedScheduler.name: LeastLoadedScheduler(),
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
