"""φ-accrual failure detector → immediate reassignment (ADR-004, M6), against a
real Postgres.

Proves the detector does what the M1 passive ``heartbeat_stale`` flag never
did: it *mutates* node status and drives recovery. The headline test shows the
detector reassigns a job whose lease is NOT yet TTL-overdue — i.e. it is strictly
faster than the lease-expiry sweep, which is what keeps recovery under the 15 s
target. It also confirms both triggers land on the *same* reassignment outcome
(REASSIGNED job, EXPIRED lease, a reliability failure, cleared wiring).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.config import get_settings
from orchestrator.models.job import Job, JobEvent, JobState
from orchestrator.models.lease import Lease, LeaseState
from orchestrator.models.node import Node, NodeStatus, NodeTelemetrySample
from orchestrator.services.failure_detection import run_failure_detection_pass
from orchestrator.services.leases import sweep_expired_leases

pytestmark = pytest.mark.asyncio


async def _make_online_node(
    session: AsyncSession,
    *,
    name: str,
    intervals: list[float],
    silence_seconds: float,
) -> Node:
    """Create an ONLINE node with real telemetry samples spaced by ``intervals``
    (seconds), whose most recent heartbeat was ``silence_seconds`` ago."""
    now = datetime.now(UTC)
    last_hb = now - timedelta(seconds=silence_seconds)
    node = Node(
        id=uuid.uuid4(),
        name=name,
        public_key="test-key",
        status=NodeStatus.ONLINE,
        hardware={"hostname": name, "os": "test", "cpu_model": "test", "cores": 4,
                  "ram_bytes": 16 * 1024**3, "gpus": []},
        agent_version="test-0.1",
        last_heartbeat_at=last_hb,
    )
    session.add(node)
    await session.flush()

    # Samples at last_hb, last_hb - i0, last_hb - i0 - i1, ... (newest → oldest).
    ts = last_hb
    times = [ts]
    for gap in reversed(intervals):
        ts = ts - timedelta(seconds=gap)
        times.append(ts)
    for sample_ts in times:
        session.add(
            NodeTelemetrySample(
                node_id=node.id,
                ts=sample_ts,
                cpu_percent=10.0,
                ram_used_bytes=1,
                ram_total_bytes=16 * 1024**3,
                gpu=None,
                rtt_ms=None,
                rtt_ewma_ms=None,
            )
        )
    await session.flush()
    return node


async def _running_job_on(
    session: AsyncSession, node: Node, *, epoch: int = 1, expires_in: float = 300.0
) -> tuple[Job, Lease]:
    """A RUNNING job with an ACTIVE rank-0 lease on ``node`` whose TTL is far in
    the future (so the TTL sweep would NOT touch it)."""
    now = datetime.now(UTC)
    job = Job(
        id=uuid.uuid4(),
        spec={"dataset": "mnist", "model": "cnn", "epochs": 3, "batch_size": 32,
              "learning_rate": 0.01, "world_size": 1, "min_gpu_mem_bytes": None},
        scheduler_name="round_robin",
        state=JobState.RUNNING,
        scheduled_node_id=node.id,
        rendezvous_node_id=node.id,
        current_lease_epoch=epoch,
        submitted_by="test",
        started_at=now,
    )
    session.add(job)
    lease = Lease(
        id=uuid.uuid4(),
        job_id=job.id,
        node_id=node.id,
        lease_epoch=epoch,
        rank=0,
        state=LeaseState.ACTIVE,
        granted_at=now,
        expires_at=now + timedelta(seconds=expires_in),
    )
    session.add(lease)
    await session.flush()
    return job, lease


async def test_detector_declares_offline_and_reassigns_before_ttl(
    session: AsyncSession,
) -> None:
    settings = get_settings()
    node = await _make_online_node(
        session, name="node-01", intervals=[2.0] * 12, silence_seconds=8.0
    )
    job, lease = await _running_job_on(session, node, expires_in=300.0)
    await session.commit()

    # The lease is NOT TTL-overdue, so the sweep is a no-op here...
    swept = await sweep_expired_leases(session, settings=settings)
    await session.commit()
    assert swept == 0
    assert (await session.get(Job, job.id)).state is JobState.RUNNING

    # ...but the φ-accrual detector declares the silent node failed and reassigns.
    declared = await run_failure_detection_pass(session, settings=settings)
    await session.commit()

    assert len(declared) == 1
    d = declared[0]
    assert d.node_name == "node-01"
    assert d.suspicion.failed is True
    assert d.suspicion.phi is not None and d.suspicion.phi >= settings.phi_accrual_threshold
    assert d.reassigned_job_ids == [job.id]

    refreshed_node = await session.get(Node, node.id)
    refreshed_job = await session.get(Job, job.id)
    refreshed_lease = await session.get(Lease, lease.id)
    assert refreshed_node.status is NodeStatus.OFFLINE
    assert refreshed_node.lease_failure_count == 1  # a real reliability failure
    assert refreshed_job.state is JobState.REASSIGNED
    assert refreshed_job.scheduled_node_id is None
    assert refreshed_job.rendezvous_node_id is None
    assert refreshed_lease.state is LeaseState.EXPIRED


async def test_reassignment_records_plain_language_timeline_event(
    session: AsyncSession,
) -> None:
    settings = get_settings()
    node = await _make_online_node(
        session, name="node-02", intervals=[2.0] * 12, silence_seconds=8.0
    )
    job, _ = await _running_job_on(session, node)
    await session.commit()

    await run_failure_detection_pass(session, settings=settings)
    await session.commit()

    events = (
        (
            await session.execute(
                select(JobEvent)
                .where(JobEvent.job_id == job.id, JobEvent.to_state == JobState.REASSIGNED)
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    detail = events[0].detail
    assert "stopped responding" in detail["message"]
    assert "node-02" in detail["message"]
    assert detail["trigger"] == "phi-accrual-detector"
    assert detail["node_name"] == "node-02"


async def test_healthy_node_is_not_declared_failed(session: AsyncSession) -> None:
    settings = get_settings()
    node = await _make_online_node(
        session, name="node-03", intervals=[2.0] * 12, silence_seconds=1.0
    )
    job, _ = await _running_job_on(session, node)
    await session.commit()

    declared = await run_failure_detection_pass(session, settings=settings)
    await session.commit()

    assert declared == []
    assert (await session.get(Node, node.id)).status is NodeStatus.ONLINE
    assert (await session.get(Job, job.id)).state is JobState.RUNNING


async def test_slow_cadence_node_tolerated_at_same_silence(session: AsyncSession) -> None:
    """A node that genuinely heartbeats every ~10 s is not declared failed at 8 s
    of silence — the threshold is relative to its own cadence, end to end."""
    settings = get_settings()
    node = await _make_online_node(
        session, name="node-04", intervals=[10.0] * 12, silence_seconds=8.0
    )
    await _running_job_on(session, node)
    await session.commit()

    declared = await run_failure_detection_pass(session, settings=settings)
    await session.commit()
    assert declared == []
    assert (await session.get(Node, node.id)).status is NodeStatus.ONLINE
