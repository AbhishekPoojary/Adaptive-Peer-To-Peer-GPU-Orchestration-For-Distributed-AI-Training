"""Epoch fencing (ADR-003): a zombie holder cannot corrupt job state.

Claim a lease (epoch 1), expire it, reschedule and re-claim (epoch 2), then have
the original holder try to renew/complete with its stale epoch 1. Both must be
rejected 409 and leave the job untouched at epoch 2.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.config import get_settings
from orchestrator.models.job import Job, JobState
from orchestrator.models.lease import Lease, LeaseState
from orchestrator.services.leases import sweep_expired_leases
from orchestrator.services.scheduling import run_scheduler_pass
from tests.helpers import (
    auth_headers,
    register_new_node,
    schedule_single_rank_job,
    send_heartbeat,
)


async def _scheduled_job(session: AsyncSession, node_id: uuid.UUID) -> Job:
    job = schedule_single_rank_job(session, node_id=node_id, submitted_by="fence-test")
    await session.commit()
    return job


async def _claim(client: AsyncClient, node_id: str, token: str) -> dict[str, object]:
    resp = await client.post(
        f"/nodes/{node_id}/leases/claim", headers=auth_headers(token)
    )
    resp.raise_for_status()
    lease = resp.json()["lease"]
    assert lease is not None
    return lease


@pytest.mark.asyncio
async def test_stale_epoch_holder_is_fenced_out(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    settings = get_settings()
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id, token = reg["node_id"], reg["access_token"]
    await send_heartbeat(api_client, node_id=node_id, token=token, gpu_util=5.0)

    job = await _scheduled_job(session, uuid.UUID(node_id))

    # Epoch 1 lease.
    lease1 = await _claim(api_client, node_id, token)
    assert lease1["lease_epoch"] == 1

    # A renew at the current epoch works.
    ok = await api_client.post(
        f"/leases/{lease1['id']}/renew",
        json={"lease_epoch": 1},
        headers=auth_headers(token),
    )
    assert ok.status_code == 200

    # Force the lease past its TTL and run the sweep -> job REASSIGNED.
    await session.execute(
        update(Lease)
        .where(Lease.id == uuid.UUID(lease1["id"]))
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()
    swept = await sweep_expired_leases(session, settings=settings)
    await session.commit()
    assert swept == 1

    # Reschedule (REASSIGNED -> SCHEDULED) and re-claim -> epoch 2.
    placed = await run_scheduler_pass(session, settings=settings)
    await session.commit()
    assert placed == 1
    lease2 = await _claim(api_client, node_id, token)
    assert lease2["lease_epoch"] == 2

    # The zombie (old lease id + stale epoch 1) is fenced out on renew...
    zombie_renew = await api_client.post(
        f"/leases/{lease1['id']}/renew",
        json={"lease_epoch": 1},
        headers=auth_headers(token),
    )
    assert zombie_renew.status_code == 409

    # ...and on complete.
    zombie_complete = await api_client.post(
        f"/leases/{lease1['id']}/complete",
        json={"lease_epoch": 1},
        headers=auth_headers(token),
    )
    assert zombie_complete.status_code == 409

    # Job state is untouched by the zombie: still LEASED at epoch 2.
    job_row = (await session.execute(select(Job).where(Job.id == job.id))).scalar_one()
    await session.refresh(job_row)
    assert job_row.state is JobState.LEASED
    assert job_row.current_lease_epoch == 2

    # lease1 stayed EXPIRED; lease2 stayed ACTIVE.
    l1 = (
        await session.execute(select(Lease).where(Lease.id == uuid.UUID(lease1["id"])))
    ).scalar_one()
    l2 = (
        await session.execute(select(Lease).where(Lease.id == uuid.UUID(lease2["id"])))
    ).scalar_one()
    assert l1.state is LeaseState.EXPIRED
    assert l2.state is LeaseState.ACTIVE


@pytest.mark.asyncio
async def test_wrong_epoch_on_current_lease_rejected(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id, token = reg["node_id"], reg["access_token"]
    await send_heartbeat(api_client, node_id=node_id, token=token, gpu_util=5.0)
    await _scheduled_job(session, uuid.UUID(node_id))
    lease = await _claim(api_client, node_id, token)

    # Current epoch is 1; renewing with epoch 2 is stale -> 409, nothing changes.
    resp = await api_client.post(
        f"/leases/{lease['id']}/renew",
        json={"lease_epoch": 2},
        headers=auth_headers(token),
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_other_node_cannot_touch_lease(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    owner, _k1 = await register_new_node(api_client, with_gpu=True)
    intruder, _k2 = await register_new_node(api_client, with_gpu=True)
    await send_heartbeat(
        api_client, node_id=owner["node_id"], token=owner["access_token"], gpu_util=5.0
    )
    await _scheduled_job(session, uuid.UUID(owner["node_id"]))
    lease = await _claim(api_client, owner["node_id"], owner["access_token"])

    resp = await api_client.post(
        f"/leases/{lease['id']}/renew",
        json={"lease_epoch": 1},
        headers=auth_headers(intruder["access_token"]),
    )
    assert resp.status_code == 403
