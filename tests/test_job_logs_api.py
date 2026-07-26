"""Tests for GET /jobs/{id}/logs (M7): cursor-based log-line pagination.

Log lines are written the same way the real WebSocket stream writes them
(``services.training.record_log_line``), against a real job/node/lease —
this pins the read side's cursor semantics against real persisted rows, not
fabricated fixtures.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.models.lease import Lease
from orchestrator.services.training import record_log_line
from tests.helpers import register_new_node, schedule_single_rank_job

pytestmark = pytest.mark.asyncio


async def _job_and_lease_id(
    api_client: AsyncClient, session: AsyncSession
) -> tuple[uuid.UUID, uuid.UUID]:
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id = uuid.UUID(reg["node_id"])
    job = schedule_single_rank_job(session, node_id=node_id, submitted_by="logs-test")
    await session.commit()
    lease_id = (
        await session.execute(select(Lease.id).where(Lease.job_id == job.id))
    ).scalar_one()
    return job.id, lease_id


async def test_logs_empty_for_job_with_no_lines_yet(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    job_id, _lease_id = await _job_and_lease_id(api_client, session)

    resp = await api_client.get(f"/jobs/{job_id}/logs")

    assert resp.status_code == 200
    body = resp.json()
    assert body["lines"] == []
    assert body["next_after"] is None


async def test_logs_404_for_unknown_job(api_client: AsyncClient) -> None:
    resp = await api_client.get(f"/jobs/{uuid.uuid4()}/logs")
    assert resp.status_code == 404


async def test_logs_returns_lines_in_order_with_cursor(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    job_id, lease_id = await _job_and_lease_id(api_client, session)

    for line in ["starting epoch 1", "loss=0.9", "loss=0.4"]:
        await record_log_line(
            session, job_id=job_id, lease_id=lease_id, stream="stdout", line=line
        )
    await session.commit()

    resp = await api_client.get(f"/jobs/{job_id}/logs")
    assert resp.status_code == 200
    body = resp.json()
    assert [ln["line"] for ln in body["lines"]] == [
        "starting epoch 1",
        "loss=0.9",
        "loss=0.4",
    ]
    first_cursor = body["next_after"]
    assert first_cursor == body["lines"][-1]["id"]

    # Polling again with the returned cursor yields nothing new yet.
    resp2 = await api_client.get(f"/jobs/{job_id}/logs", params={"after": first_cursor})
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["lines"] == []
    assert body2["next_after"] == first_cursor

    # A new line appended after that poll is the only thing the next poll sees.
    await record_log_line(
        session, job_id=job_id, lease_id=lease_id, stream="stderr", line="epoch 1 done"
    )
    await session.commit()

    resp3 = await api_client.get(f"/jobs/{job_id}/logs", params={"after": first_cursor})
    assert resp3.status_code == 200
    body3 = resp3.json()
    assert len(body3["lines"]) == 1
    assert body3["lines"][0]["line"] == "epoch 1 done"
    assert body3["lines"][0]["stream"] == "stderr"
    assert body3["next_after"] == body3["lines"][0]["id"]


async def test_logs_limit_is_respected(api_client: AsyncClient, session: AsyncSession) -> None:
    job_id, lease_id = await _job_and_lease_id(api_client, session)

    for i in range(5):
        await record_log_line(
            session, job_id=job_id, lease_id=lease_id, stream="stdout", line=f"line {i}"
        )
    await session.commit()

    resp = await api_client.get(f"/jobs/{job_id}/logs", params={"limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert [ln["line"] for ln in body["lines"]] == ["line 0", "line 1"]
    assert body["next_after"] == body["lines"][-1]["id"]
