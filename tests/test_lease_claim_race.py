"""THE race test (ADR-003): concurrent claims never double-assign.

50 simulated agents (10 nodes x 5 concurrent claim calls each) contend for 10
jobs, one scheduled to each node, against real Postgres. The guarantee under
test is Postgres ``SELECT ... FOR UPDATE SKIP LOCKED`` plus the partial unique
index — so this cannot pass on SQLite and is meaningless without genuine
concurrency. Expected outcome: exactly 10 leases granted, 40 clean empty-handed
responses, and no job ever holding two ACTIVE leases.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.models.job import Job, JobState
from orchestrator.models.lease import Lease, LeaseState
from tests.helpers import auth_headers, register_new_node

_NODES = 10
_CLAIMS_PER_NODE = 5  # 10 x 5 = 50 concurrent attempts
_SPEC = {
    "dataset": "mnist",
    "model": "cnn",
    "epochs": 1,
    "batch_size": 32,
    "learning_rate": 0.01,
    "world_size": 1,
    "min_gpu_mem_bytes": None,
}


async def _scheduled_job_for(session: AsyncSession, node_id: uuid.UUID) -> Job:
    job = Job(
        spec=_SPEC,
        scheduler_name="round_robin",
        state=JobState.SCHEDULED,
        scheduled_node_id=node_id,
        submitted_by="race-test",
    )
    session.add(job)
    return job


@pytest.mark.asyncio
async def test_fifty_concurrent_claims_grant_exactly_ten_leases(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    # 10 nodes, each with exactly one job scheduled to it.
    nodes: list[tuple[str, str]] = []  # (node_id, token)
    for _ in range(_NODES):
        reg, _key = await register_new_node(api_client)
        nodes.append((reg["node_id"], reg["access_token"]))
        await _scheduled_job_for(session, uuid.UUID(reg["node_id"]))
    await session.commit()

    async def claim(node_id: str, token: str) -> bool:
        resp = await api_client.post(
            f"/nodes/{node_id}/leases/claim", headers=auth_headers(token)
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["lease"] is not None

    # 50 concurrent claims: 5 per node, all fired at once.
    tasks = [
        claim(node_id, token)
        for node_id, token in nodes
        for _ in range(_CLAIMS_PER_NODE)
    ]
    results = await asyncio.gather(*tasks)

    granted = sum(1 for r in results if r)
    empty = sum(1 for r in results if not r)
    assert len(results) == _NODES * _CLAIMS_PER_NODE == 50
    assert granted == _NODES == 10, f"expected 10 grants, got {granted}"
    assert empty == 40, f"expected 40 empty-handed, got {empty}"

    # DB truth: exactly 10 ACTIVE leases, and NO job with more than one.
    active_total = (
        await session.execute(
            select(func.count())
            .select_from(Lease)
            .where(Lease.state == LeaseState.ACTIVE)
        )
    ).scalar_one()
    assert active_total == 10

    dupes = (
        await session.execute(
            select(Lease.job_id, func.count())
            .where(Lease.state == LeaseState.ACTIVE)
            .group_by(Lease.job_id)
            .having(func.count() > 1)
        )
    ).all()
    assert dupes == [], f"a job has more than one ACTIVE lease: {dupes}"

    # Every claimed job is LEASED at epoch 1.
    leased = (
        await session.execute(
            select(func.count()).select_from(Job).where(Job.state == JobState.LEASED)
        )
    ).scalar_one()
    assert leased == 10


@pytest.mark.asyncio
async def test_partial_unique_index_blocks_second_active_lease(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Defense in depth: the DB itself refuses a second ACTIVE lease per job."""
    reg, _key = await register_new_node(api_client)
    node_id = uuid.UUID(reg["node_id"])
    job = await _scheduled_job_for(session, node_id)
    await session.commit()

    from datetime import UTC, datetime, timedelta

    exp = datetime.now(UTC) + timedelta(seconds=30)
    session.add(
        Lease(job_id=job.id, node_id=node_id, lease_epoch=1,
              state=LeaseState.ACTIVE, expires_at=exp)
    )
    session.add(
        Lease(job_id=job.id, node_id=node_id, lease_epoch=2,
              state=LeaseState.ACTIVE, expires_at=exp)
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()
