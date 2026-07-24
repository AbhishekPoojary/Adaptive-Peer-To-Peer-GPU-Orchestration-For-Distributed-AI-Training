"""WebSocket log/metric stream authorization (ADR-002, M4).

``authorize_stream`` is the epoch-fencing/auth decision factored out of the
WebSocket transport handling (``orchestrator.api.streaming.lease_stream``) so
it can be exercised directly against a real Postgres-backed session without
needing an actual WebSocket connection. Covers: valid stream, missing/invalid
token, wrong node, unknown lease, and — the core ADR-002 guarantee — a stale
epoch (a zombie agent from a reassigned job) being rejected.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.streaming import StreamAuthOutcome, authorize_stream
from orchestrator.core.config import get_settings
from orchestrator.core.security import create_node_jwt
from orchestrator.models.job import Job
from orchestrator.models.lease import Lease
from orchestrator.services.leases import sweep_expired_leases
from orchestrator.services.scheduling import run_scheduler_pass
from tests.helpers import (
    auth_headers,
    register_new_node,
    schedule_single_rank_job,
    send_heartbeat,
)


async def _scheduled_job(session: AsyncSession, node_id: uuid.UUID) -> Job:
    job = schedule_single_rank_job(
        session,
        node_id=node_id,
        spec={
            "dataset": "mnist",
            "model": "small_cnn",
            "epochs": 1,
            "batch_size": 32,
            "learning_rate": 0.01,
            "world_size": 1,
            "min_gpu_mem_bytes": None,
        },
        submitted_by="stream-auth-test",
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
async def test_valid_stream_is_authorized(api_client: AsyncClient, session: AsyncSession) -> None:
    settings = get_settings()
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id, token = reg["node_id"], reg["access_token"]
    await send_heartbeat(api_client, node_id=node_id, token=token, gpu_util=5.0)
    await _scheduled_job(session, uuid.UUID(node_id))
    lease = await _claim(api_client, node_id, token)

    result = await authorize_stream(
        session,
        node_id=uuid.UUID(node_id),
        lease_id=uuid.UUID(lease["id"]),
        epoch=lease["lease_epoch"],
        authorization_header=f"Bearer {token}",
        settings=settings,
    )
    assert result.outcome is StreamAuthOutcome.OK
    assert result.job_id is not None


@pytest.mark.asyncio
async def test_missing_bearer_token_is_unauthorized(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    settings = get_settings()
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id, token = reg["node_id"], reg["access_token"]
    await send_heartbeat(api_client, node_id=node_id, token=token, gpu_util=5.0)
    await _scheduled_job(session, uuid.UUID(node_id))
    lease = await _claim(api_client, node_id, token)

    result = await authorize_stream(
        session,
        node_id=uuid.UUID(node_id),
        lease_id=uuid.UUID(lease["id"]),
        epoch=lease["lease_epoch"],
        authorization_header=None,
        settings=settings,
    )
    assert result.outcome is StreamAuthOutcome.UNAUTHORIZED


@pytest.mark.asyncio
async def test_invalid_token_is_unauthorized(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    settings = get_settings()
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id, token = reg["node_id"], reg["access_token"]
    await send_heartbeat(api_client, node_id=node_id, token=token, gpu_util=5.0)
    await _scheduled_job(session, uuid.UUID(node_id))
    lease = await _claim(api_client, node_id, token)

    result = await authorize_stream(
        session,
        node_id=uuid.UUID(node_id),
        lease_id=uuid.UUID(lease["id"]),
        epoch=lease["lease_epoch"],
        authorization_header="Bearer not-a-real-token",
        settings=settings,
    )
    assert result.outcome is StreamAuthOutcome.UNAUTHORIZED


@pytest.mark.asyncio
async def test_wrong_node_token_is_forbidden(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    settings = get_settings()
    owner, _k1 = await register_new_node(api_client, with_gpu=True)
    intruder, _k2 = await register_new_node(api_client, with_gpu=True)
    await send_heartbeat(
        api_client, node_id=owner["node_id"], token=owner["access_token"], gpu_util=5.0
    )
    await _scheduled_job(session, uuid.UUID(owner["node_id"]))
    lease = await _claim(api_client, owner["node_id"], owner["access_token"])

    result = await authorize_stream(
        session,
        node_id=uuid.UUID(owner["node_id"]),
        lease_id=uuid.UUID(lease["id"]),
        epoch=lease["lease_epoch"],
        authorization_header=f"Bearer {intruder['access_token']}",
        settings=settings,
    )
    assert result.outcome is StreamAuthOutcome.FORBIDDEN


@pytest.mark.asyncio
async def test_unknown_lease_is_not_found(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    settings = get_settings()
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id, token = reg["node_id"], reg["access_token"]
    await send_heartbeat(api_client, node_id=node_id, token=token, gpu_util=5.0)

    result = await authorize_stream(
        session,
        node_id=uuid.UUID(node_id),
        lease_id=uuid.uuid4(),
        epoch=1,
        authorization_header=f"Bearer {token}",
        settings=settings,
    )
    assert result.outcome is StreamAuthOutcome.NOT_FOUND


@pytest.mark.asyncio
async def test_stale_epoch_from_reassigned_job_is_rejected(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The core ADR-002 guarantee: a zombie agent holding a stale epoch after
    its job was reassigned (lease expired -> swept -> rescheduled -> re-claimed
    at a new epoch) cannot open a stream into the new epoch's channel."""
    settings = get_settings()
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id, token = reg["node_id"], reg["access_token"]
    await send_heartbeat(api_client, node_id=node_id, token=token, gpu_util=5.0)
    await _scheduled_job(session, uuid.UUID(node_id))

    lease1 = await _claim(api_client, node_id, token)
    assert lease1["lease_epoch"] == 1

    # Force the lease past its TTL, sweep it (-> job REASSIGNED).
    await session.execute(
        update(Lease)
        .where(Lease.id == uuid.UUID(lease1["id"]))
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()
    swept = await sweep_expired_leases(session, settings=settings)
    await session.commit()
    assert swept == 1

    # Reschedule and re-claim -> epoch 2.
    placed = await run_scheduler_pass(session, settings=settings)
    await session.commit()
    assert placed == 1
    lease2 = await _claim(api_client, node_id, token)
    assert lease2["lease_epoch"] == 2

    # The zombie tries to open a stream with its old lease id + stale epoch 1.
    result = await authorize_stream(
        session,
        node_id=uuid.UUID(node_id),
        lease_id=uuid.UUID(lease1["id"]),
        epoch=1,
        authorization_header=f"Bearer {token}",
        settings=settings,
    )
    assert result.outcome is StreamAuthOutcome.STALE_EPOCH

    # Meanwhile the real current epoch (2) on the new lease is authorized.
    ok = await authorize_stream(
        session,
        node_id=uuid.UUID(node_id),
        lease_id=uuid.UUID(lease2["id"]),
        epoch=2,
        authorization_header=f"Bearer {token}",
        settings=settings,
    )
    assert ok.outcome is StreamAuthOutcome.OK


@pytest.mark.asyncio
async def test_wrong_epoch_on_current_lease_is_rejected(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    settings = get_settings()
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id, token = reg["node_id"], reg["access_token"]
    await send_heartbeat(api_client, node_id=node_id, token=token, gpu_util=5.0)
    await _scheduled_job(session, uuid.UUID(node_id))
    lease = await _claim(api_client, node_id, token)

    result = await authorize_stream(
        session,
        node_id=uuid.UUID(node_id),
        lease_id=uuid.UUID(lease["id"]),
        epoch=lease["lease_epoch"] + 1,
        authorization_header=f"Bearer {token}",
        settings=settings,
    )
    assert result.outcome is StreamAuthOutcome.STALE_EPOCH


@pytest.mark.asyncio
async def test_expired_jwt_is_unauthorized(api_client: AsyncClient, session: AsyncSession) -> None:
    """A token that fails signature/expiry verification (ADR-008) is rejected
    on the stream exactly like on the REST endpoints."""
    settings = get_settings()
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id, token = reg["node_id"], reg["access_token"]
    await send_heartbeat(api_client, node_id=node_id, token=token, gpu_util=5.0)
    await _scheduled_job(session, uuid.UUID(node_id))
    lease = await _claim(api_client, node_id, token)

    expired_token = create_node_jwt(
        node_id=node_id,
        signing_key=settings.jwt_signing_key,
        ttl_seconds=-10,
    )
    result = await authorize_stream(
        session,
        node_id=uuid.UUID(node_id),
        lease_id=uuid.UUID(lease["id"]),
        epoch=lease["lease_epoch"],
        authorization_header=f"Bearer {expired_token}",
        settings=settings,
    )
    assert result.outcome is StreamAuthOutcome.UNAUTHORIZED
