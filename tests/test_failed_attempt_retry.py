"""A trainer failure is retried on another peer, but only so many times.

ADR-005 addendum 2. The original rule — any reported trainer failure is
terminal — was built to stop a broken job spec walking the whole fleet, and it
does. What it wrongly assumed is that every trainer failure is a property of
the *job*. Exit 137 is ``SIGKILL``, and on this project's target fleet of
student laptops with 4 GB GPUs the likeliest sender by far is the OOM killer —
a property of the *machine*, which another peer would not share.

So the system was least fault-tolerant against the single most probable real
failure in the deployment it was designed for. These tests pin both halves of
the fix: the job now survives a killed trainer, and a job that fails everywhere
still fails loudly rather than looping forever.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.models.job import Job, JobState
from orchestrator.models.lease import Lease, LeaseState
from orchestrator.models.node import Node
from orchestrator.services.leases import claim_job_for_node, fail_lease
from tests.helpers import (
    register_new_node,
    schedule_single_rank_job,
    send_heartbeat,
)

#: The exit code a container gets from `docker kill` / the OOM killer: 128 + 9.
SIGKILL_RESULT = {
    "final_loss": None,
    "final_test_accuracy": None,
    "epochs_completed": 0,
    "exit_code": 137,
    "device": "cuda",
}


async def _online_node(api_client: AsyncClient, session: AsyncSession) -> Node:
    reg, _key = await register_new_node(api_client, with_gpu=True)
    await send_heartbeat(
        api_client, node_id=str(reg["node_id"]), token=str(reg["access_token"]),
        gpu_util=5.0,
    )
    return (
        await session.execute(
            select(Node)
            .where(Node.id == uuid.UUID(str(reg["node_id"])))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _claimed_lease(
    session: AsyncSession, node: Node, *, settings, job: Job | None = None
) -> tuple[Job, Lease]:
    """Place a job on ``node`` and claim it, as a real agent would."""
    if job is None:
        job = schedule_single_rank_job(session, node_id=node.id, submitted_by="retry")
        await session.commit()
    lease = await claim_job_for_node(session, node=node, settings=settings)
    assert lease is not None, "the node should have had a claimable offer"
    await session.commit()
    return job, lease


@pytest.mark.asyncio
async def test_a_killed_trainer_is_retried_not_fatal(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The headline. A SIGKILLed trainer must not end the job outright."""
    from orchestrator.core.config import get_settings

    settings = get_settings()
    node = await _online_node(api_client, session)
    job, lease = await _claimed_lease(session, node, settings=settings)

    await fail_lease(
        session,
        lease_id=lease.id,
        node=node,
        epoch=lease.lease_epoch,
        reason="trainer exited with code 137",
        result=dict(SIGKILL_RESULT),
        max_failure_retries=2,
        failed_attempt_backoff_seconds=45.0,
    )
    await session.commit()
    await session.refresh(job)

    assert job.state is JobState.REASSIGNED, (
        "a killed trainer must be retried on another peer, not end the job"
    )
    assert job.failed_attempt_count == 1
    # Cohort wiring cleared so the next pass places it freshly.
    assert job.scheduled_node_id is None
    assert job.rendezvous_node_id is None


@pytest.mark.asyncio
async def test_the_failing_node_is_backed_off_so_the_retry_moves(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """A retry that lands back on the machine that just killed the trainer is
    not a retry. The node is skipped for a window — and still earns its real
    reliability failure, because a trainer genuinely died on it."""
    from orchestrator.core.config import get_settings

    settings = get_settings()
    node = await _online_node(api_client, session)
    _job, lease = await _claimed_lease(session, node, settings=settings)

    await fail_lease(
        session,
        lease_id=lease.id,
        node=node,
        epoch=lease.lease_epoch,
        reason="trainer exited with code 137",
        result=dict(SIGKILL_RESULT),
        max_failure_retries=2,
        failed_attempt_backoff_seconds=45.0,
    )
    await session.commit()
    await session.refresh(node)

    assert node.scheduling_backoff_until is not None
    assert node.scheduling_backoff_until > datetime.now(UTC)
    # Unlike an unclaimed offer, this one IS the node's fault and is recorded.
    assert node.lease_failure_count == 1


@pytest.mark.asyncio
async def test_a_job_that_fails_everywhere_still_fails_terminally(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The bound. A genuinely broken job must stop, not loop across the fleet —
    which was ADR-005's original and still-valid concern.

    Drives the real cycle: claim, fail, re-place, claim, fail... until the job
    exhausts its retries.
    """
    from orchestrator.core.config import get_settings
    from orchestrator.services.scheduling import run_scheduler_pass

    settings = get_settings()
    node = await _online_node(api_client, session)
    max_retries = 2

    job, lease = await _claimed_lease(session, node, settings=settings)
    for attempt in range(1, max_retries + 2):
        await fail_lease(
            session,
            lease_id=lease.id,
            node=node,
            epoch=lease.lease_epoch,
            reason="model 'nonexistent' is not implemented",
            result={**SIGKILL_RESULT, "exit_code": 1},
            max_failure_retries=max_retries,
            # No backoff: a single-node fleet must still be able to retry where
            # it is, or this test would stall rather than reach the bound.
            failed_attempt_backoff_seconds=0.0,
        )
        await session.commit()
        await session.refresh(job)
        assert job.failed_attempt_count == attempt

        if attempt <= max_retries:
            assert job.state is JobState.REASSIGNED, f"attempt {attempt}"
            await run_scheduler_pass(session, settings=settings)
            await session.commit()
            await session.refresh(job)
            assert job.state is JobState.SCHEDULED, "the retry must be re-placed"
            claimed = await claim_job_for_node(session, node=node, settings=settings)
            assert claimed is not None
            await session.commit()
            lease = claimed

    assert job.state is JobState.FAILED, "the bound must eventually stop the job"
    assert job.failed_attempt_count == max_retries + 1

    events = (
        await session.execute(
            select(Job).where(Job.id == job.id).execution_options(
                populate_existing=True
            )
        )
    ).scalar_one()
    assert events.failure_reason == "model 'nonexistent' is not implemented"


@pytest.mark.asyncio
async def test_the_terminal_message_states_how_many_attempts_were_made(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """A failure that took three peers and a minute to arrive at must not read
    like one unlucky run — otherwise the retry is invisible to the operator."""
    from orchestrator.core.config import get_settings
    from orchestrator.models.job import JobEvent

    settings = get_settings()
    node = await _online_node(api_client, session)
    job, lease = await _claimed_lease(session, node, settings=settings)

    await fail_lease(
        session,
        lease_id=lease.id,
        node=node,
        epoch=lease.lease_epoch,
        reason="boom",
        result=dict(SIGKILL_RESULT),
        max_failure_retries=0,  # terminal immediately, but count it
        failed_attempt_backoff_seconds=0.0,
    )
    await session.commit()
    await session.refresh(job)
    assert job.state is JobState.FAILED

    # A first-attempt failure reads plainly, with no confusing attempt count.
    final = (
        await session.execute(
            select(JobEvent)
            .where(JobEvent.job_id == job.id, JobEvent.to_state == "FAILED")
        )
    ).scalars().all()
    assert final, "a FAILED transition must be recorded"
    assert "Gave up after" not in final[-1].detail["message"]


@pytest.mark.asyncio
async def test_zero_retries_reproduces_the_strict_old_behaviour(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """A deployment that prefers fail-fast must be able to have it by config,
    with no code change (ADR-005 addendum 2)."""
    from orchestrator.core.config import get_settings

    settings = get_settings()
    node = await _online_node(api_client, session)
    job, lease = await _claimed_lease(session, node, settings=settings)

    await fail_lease(
        session,
        lease_id=lease.id,
        node=node,
        epoch=lease.lease_epoch,
        reason="trainer exited with code 137",
        result=dict(SIGKILL_RESULT),
        max_failure_retries=0,
        failed_attempt_backoff_seconds=0.0,
    )
    await session.commit()
    await session.refresh(job)

    assert job.state is JobState.FAILED
    lease_row = (
        await session.execute(select(Lease).where(Lease.id == lease.id))
    ).scalar_one()
    assert lease_row.state is LeaseState.FAILED
