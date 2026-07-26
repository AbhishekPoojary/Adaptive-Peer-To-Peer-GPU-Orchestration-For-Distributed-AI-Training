"""Periodic background loops: the scheduler pass and the lease expiry sweep.

Two asyncio tasks the orchestrator app owns for its lifetime. A submit also
triggers an immediate scheduler pass (``trigger_scheduler_pass``) so placement
isn't gated on the loop cadence.

Scheduler passes are serialised by ``_scheduler_lock`` so two overlapping passes
(loop vs. submit-triggered) can't pick the same free node for two jobs. Each
loop opens its own short-lived session and commits; a failing iteration is
logged and the loop continues rather than dying silently.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from orchestrator.core.config import Settings
from orchestrator.core.db import session_scope
from orchestrator.services.failure_detection import run_failure_detection_pass
from orchestrator.services.leases import sweep_expired_leases
from orchestrator.services.scheduling import run_scheduler_pass

logger = logging.getLogger("orchestrator.loops")

# Ensures scheduler passes never run concurrently within this process.
_scheduler_lock = asyncio.Lock()


async def trigger_scheduler_pass(settings: Settings) -> int:
    """Run one scheduler pass under the process-wide lock and commit."""
    async with _scheduler_lock, session_scope() as session:
        placed = await run_scheduler_pass(session, settings=settings)
        await session.commit()
        return placed


async def _scheduler_loop(settings: Settings) -> None:
    interval = settings.scheduler_pass_interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            placed = await trigger_scheduler_pass(settings)
            if placed:
                logger.info("scheduler pass placed %d job(s)", placed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduler pass failed; will retry next interval")


async def _sweep_loop(settings: Settings) -> None:
    interval = settings.lease_sweep_interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            async with session_scope() as session:
                expired = await sweep_expired_leases(session, settings=settings)
                await session.commit()
            if expired:
                logger.info("expiry sweep expired %d lease(s); rescheduling", expired)
                # Immediately try to re-place the jobs that just came free.
                await trigger_scheduler_pass(settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("lease expiry sweep failed; will retry next interval")


async def _failure_detector_loop(settings: Settings) -> None:
    """The M6 φ-accrual failure detector (ADR-004): declare silent ONLINE nodes
    OFFLINE and immediately reassign their jobs — faster than the lease-TTL
    sweep. A declared failure triggers an immediate scheduler pass so the
    reassigned job is re-placed without waiting for the scheduler cadence, which
    is what keeps total recovery inside the 15 s target."""
    interval = settings.failure_detector_interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            async with session_scope() as session:
                declared = await run_failure_detection_pass(session, settings=settings)
                await session.commit()
            if declared:
                for d in declared:
                    logger.warning(
                        "failure detector declared node %s OFFLINE: %s; reassigned %d job(s)",
                        d.node_name,
                        d.suspicion.reason,
                        len(d.reassigned_job_ids),
                    )
                # Re-place the just-reassigned jobs now, not on the next cadence.
                await trigger_scheduler_pass(settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("failure detection pass failed; will retry next interval")


def start_background_loops(settings: Settings) -> list[asyncio.Task[None]]:
    """Start the scheduler, sweep, and failure-detector loops. Returns the
    created tasks."""
    return [
        asyncio.create_task(_scheduler_loop(settings), name="scheduler-loop"),
        asyncio.create_task(_sweep_loop(settings), name="lease-sweep-loop"),
        asyncio.create_task(
            _failure_detector_loop(settings), name="failure-detector-loop"
        ),
    ]


async def stop_background_loops(tasks: list[asyncio.Task[None]]) -> None:
    """Cancel the background loops and wait for them to unwind."""
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
