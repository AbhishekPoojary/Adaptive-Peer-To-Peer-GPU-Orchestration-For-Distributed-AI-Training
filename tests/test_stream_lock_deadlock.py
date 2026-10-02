"""A lease renewal must not block a log or metric insert for the same job.

Observed in a real run: a metric insert from the trainer's stream deadlocked
with a concurrent lease renewal. Postgres killed the insert, the stream handler
died with it, and every later log line and metric of that job was lost while
training carried on. The renewal had locked lease-then-job ``FOR UPDATE``;
the insert's foreign-key checks take ``KEY SHARE`` on job-then-lease, and
``FOR UPDATE`` conflicts with ``KEY SHARE``.

Real Postgres, two real sessions, the real ``renew_lease``: the renewal's locks
are held open while the insert runs, which is exactly the window the deadlock
needed. With ``FOR NO KEY UPDATE`` the insert does not wait at all.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.config import get_settings
from orchestrator.core.db import get_sessionmaker
from orchestrator.models.node import Node
from orchestrator.services.leases import renew_lease
from orchestrator.services.training import record_metric
from tests.helpers import (
    auth_headers,
    register_new_node,
    schedule_single_rank_job,
    send_heartbeat,
)


@pytest.mark.asyncio
async def test_metric_insert_is_not_blocked_by_a_held_renewal(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id, token = reg["node_id"], reg["access_token"]
    await send_heartbeat(api_client, node_id=node_id, token=token, gpu_util=5.0)
    schedule_single_rank_job(session, node_id=uuid.UUID(node_id), submitted_by="lock-test")
    await session.commit()
    claim = await api_client.post(f"/nodes/{node_id}/leases/claim", headers=auth_headers(token))
    lease = claim.json()["lease"]
    lease_id, job_id = uuid.UUID(lease["id"]), uuid.UUID(lease["job_id"])

    maker = get_sessionmaker()
    async with maker() as renewing, maker() as streaming:
        node_query = select(Node).where(Node.id == uuid.UUID(node_id))
        node = (await renewing.execute(node_query)).scalar_one()
        # Takes and HOLDS the lease and job row locks (renew_lease leaves the
        # commit to its caller).
        await renew_lease(
            renewing, lease_id=lease_id, node=node, epoch=1, settings=get_settings()
        )

        async def insert_metric() -> None:
            await record_metric(
                streaming, job_id=job_id, lease_id=lease_id, epoch=1, loss=0.5,
                test_accuracy=0.9, step=10,
            )
            await streaming.commit()

        try:
            await asyncio.wait_for(insert_metric(), timeout=5.0)
        except TimeoutError:
            pytest.fail(
                "the metric insert waited on the renewal's row locks; those locks "
                "conflict with foreign-key checks and can deadlock the stream"
            )
        finally:
            await renewing.commit()
