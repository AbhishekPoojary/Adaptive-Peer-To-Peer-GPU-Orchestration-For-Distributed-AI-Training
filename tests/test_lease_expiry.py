"""Lease expiry sweep (ADR-003 / ADR-009): timeouts reassign and count once.

When an ACTIVE lease passes its TTL, the sweep must expire it, move the job to
REASSIGNED (then re-placeable), and increment the node's real failure count by
exactly one — a timeout is an earned reliability signal, not a fabricated one.
Also proves the natural TTL path with a short real elapse via the service layer.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.config import get_settings
from orchestrator.models.job import Job, JobEvent, JobState
from orchestrator.models.lease import Lease, LeaseState
from orchestrator.models.node import Node
from orchestrator.services.leases import (
    claim_job_for_node,
    sweep_expired_leases,
)
from orchestrator.services.scheduling import run_scheduler_pass
from tests.helpers import auth_headers, register_new_node, send_heartbeat


async def _scheduled_job(session: AsyncSession, node_id: uuid.UUID) -> Job:
    job = Job(
        spec={"dataset": "mnist", "model": "cnn", "epochs": 1, "batch_size": 32,
              "learning_rate": 0.01, "world_size": 1, "min_gpu_mem_bytes": None},
        scheduler_name="round_robin",
        state=JobState.SCHEDULED,
        scheduled_node_id=node_id,
        submitted_by="expiry-test",
    )
    session.add(job)
    await session.commit()
    return job


@pytest.mark.asyncio
async def test_expiry_reassigns_job_and_counts_one_failure(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    settings = get_settings()
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id = uuid.UUID(reg["node_id"])
    await send_heartbeat(
        api_client, node_id=reg["node_id"], token=reg["access_token"], gpu_util=5.0
    )
    job = await _scheduled_job(session, node_id)

    # Claim via HTTP -> ACTIVE lease, job LEASED.
    resp = await api_client.post(
        f"/nodes/{reg['node_id']}/leases/claim", headers=auth_headers(reg["access_token"])
    )
    lease = resp.json()["lease"]
    assert lease is not None

    node_before = (await session.execute(select(Node).where(Node.id == node_id))).scalar_one()
    await session.refresh(node_before)
    assert node_before.lease_failure_count == 0

    # Force the TTL to have elapsed, then sweep.
    await session.execute(
        update(Lease)
        .where(Lease.id == uuid.UUID(lease["id"]))
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()
    swept = await sweep_expired_leases(session, settings=settings)
    await session.commit()
    assert swept == 1

    job_row = (await session.execute(select(Job).where(Job.id == job.id))).scalar_one()
    await session.refresh(job_row)
    assert job_row.state is JobState.REASSIGNED
    assert job_row.scheduled_node_id is None

    node_after = (await session.execute(select(Node).where(Node.id == node_id))).scalar_one()
    await session.refresh(node_after)
    assert node_after.lease_failure_count == 1  # incremented by EXACTLY one
    assert node_after.lease_success_count == 0

    lease_row = (
        await session.execute(select(Lease).where(Lease.id == uuid.UUID(lease["id"])))
    ).scalar_one()
    assert lease_row.state is LeaseState.EXPIRED

    # A scheduler pass re-places the REASSIGNED job.
    placed = await run_scheduler_pass(session, settings=settings)
    await session.commit()
    assert placed == 1
    await session.refresh(job_row)
    assert job_row.state is JobState.SCHEDULED

    # Timeline records the REASSIGNED transition with a human-readable message.
    events = (
        await session.execute(
            select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.id)
        )
    ).scalars().all()
    to_states = [e.to_state for e in events]
    assert JobState.REASSIGNED in to_states
    reassigned_evt = next(e for e in events if e.to_state is JobState.REASSIGNED)
    assert "expired" in reassigned_evt.detail["message"].lower()


@pytest.mark.asyncio
async def test_natural_short_ttl_elapse_expires_lease(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The natural path: a real (short) TTL genuinely elapsing, no timestamp
    surgery — the sweep compares real wall-clock times."""
    settings = get_settings().model_copy(update={"lease_ttl_seconds": 1})
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id = uuid.UUID(reg["node_id"])
    await send_heartbeat(
        api_client, node_id=reg["node_id"], token=reg["access_token"], gpu_util=5.0
    )
    job = await _scheduled_job(session, node_id)

    node = (await session.execute(select(Node).where(Node.id == node_id))).scalar_one()
    lease = await claim_job_for_node(session, node=node, settings=settings)
    await session.commit()
    assert lease is not None

    await asyncio.sleep(1.3)  # let the 1s TTL genuinely elapse

    swept = await sweep_expired_leases(session, settings=settings)
    await session.commit()
    assert swept == 1
    job_row = (await session.execute(select(Job).where(Job.id == job.id))).scalar_one()
    await session.refresh(job_row)
    assert job_row.state is JobState.REASSIGNED
