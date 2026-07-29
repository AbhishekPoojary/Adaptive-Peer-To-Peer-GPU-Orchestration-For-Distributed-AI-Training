"""Lease happy-path lifecycle: renew, complete, fail, and earned reliability.

Reliability counters move only on real outcomes: complete -> success +1,
fail -> failure +1. The job's audit trail and terminal state follow.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.models.job import Job, JobState
from orchestrator.models.lease import Lease, LeaseState
from orchestrator.models.node import Node
from tests.helpers import (
    auth_headers,
    register_new_node,
    schedule_single_rank_job,
    send_heartbeat,
)


async def _scheduled_job(session: AsyncSession, node_id: uuid.UUID) -> Job:
    job = schedule_single_rank_job(
        session, node_id=node_id, submitted_by="lifecycle-test"
    )
    await session.commit()
    return job


async def _claim(client: AsyncClient, node_id: str, token: str) -> dict[str, object]:
    resp = await client.post(f"/nodes/{node_id}/leases/claim", headers=auth_headers(token))
    resp.raise_for_status()
    lease = resp.json()["lease"]
    assert lease is not None
    return lease


@pytest.mark.asyncio
async def test_renew_thrice_then_complete(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id, token = reg["node_id"], reg["access_token"]
    await send_heartbeat(api_client, node_id=node_id, token=token, gpu_util=5.0)
    job = await _scheduled_job(session, uuid.UUID(node_id))
    lease = await _claim(api_client, node_id, token)

    prev_expiry = lease["expires_at"]
    for _ in range(3):
        r = await api_client.post(
            f"/leases/{lease['id']}/renew",
            json={"lease_epoch": 1},
            headers=auth_headers(token),
        )
        assert r.status_code == 200
        assert r.json()["renewed_at"] is not None
        assert r.json()["expires_at"] >= prev_expiry
        prev_expiry = r.json()["expires_at"]

    done = await api_client.post(
        f"/leases/{lease['id']}/complete",
        json={"lease_epoch": 1},
        headers=auth_headers(token),
    )
    assert done.status_code == 200
    assert done.json()["state"] == "COMPLETED"

    job_row = (await session.execute(select(Job).where(Job.id == job.id))).scalar_one()
    await session.refresh(job_row)
    assert job_row.state is JobState.COMPLETED
    assert job_row.finished_at is not None

    node = (await session.execute(select(Node).where(Node.id == uuid.UUID(node_id)))).scalar_one()
    await session.refresh(node)
    assert node.lease_success_count == 1
    assert node.lease_failure_count == 0


@pytest.mark.asyncio
async def test_fail_records_reason_and_failure_count(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id, token = reg["node_id"], reg["access_token"]
    await send_heartbeat(api_client, node_id=node_id, token=token, gpu_util=5.0)
    job = await _scheduled_job(session, uuid.UUID(node_id))
    lease = await _claim(api_client, node_id, token)

    resp = await api_client.post(
        f"/leases/{lease['id']}/fail",
        json={"lease_epoch": 1, "reason": "execution-not-implemented"},
        headers=auth_headers(token),
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "FAILED"

    job_row = (await session.execute(select(Job).where(Job.id == job.id))).scalar_one()
    await session.refresh(job_row)
    # The *lease* failed terminally; the *job* is retried on another peer while
    # it has retries left (ADR-005 addendum 2). The reason and the node's
    # reliability failure are recorded either way — those are what this test
    # exists to pin, and they are unchanged.
    assert job_row.state is JobState.REASSIGNED
    assert job_row.failed_attempt_count == 1
    assert job_row.failure_reason == "execution-not-implemented"

    node = (await session.execute(select(Node).where(Node.id == uuid.UUID(node_id)))).scalar_one()
    await session.refresh(node)
    assert node.lease_failure_count == 1
    assert node.lease_success_count == 0

    lease_row = (
        await session.execute(select(Lease).where(Lease.id == uuid.UUID(lease["id"])))
    ).scalar_one()
    assert lease_row.state is LeaseState.FAILED


@pytest.mark.asyncio
async def test_claim_empty_when_nothing_scheduled(api_client: AsyncClient) -> None:
    reg, _key = await register_new_node(api_client, with_gpu=True)
    resp = await api_client.post(
        f"/nodes/{reg['node_id']}/leases/claim",
        headers=auth_headers(reg["access_token"]),
    )
    assert resp.status_code == 200
    assert resp.json()["lease"] is None
