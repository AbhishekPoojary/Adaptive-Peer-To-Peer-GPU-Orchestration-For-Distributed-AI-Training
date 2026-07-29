"""A node that lets an offer lapse is not re-offered immediately (M7.1c).

Defence in depth against the offer-thrash observed in the wild: job d2fc2a9f
burned 7 reassignment epochs in 108 seconds against a single node whose agent
was stuck on an orphaned container. Every cycle looked like this — the node was
ONLINE, heartbeating, and idle by every measure the scheduler had, so it was
immediately handed the same work again, which lapsed again.

M7.1b fixed the root cause (the agent now abandons its container when fenced
out). This is the second line: a node that demonstrably could not pick up the
last offer is skipped for a moment, whatever the reason.

The backoff is deliberately *not* a reliability signal. An unclaimed offer is
blameless (ADR-003 addendum) — the node's counters must stay untouched, and
these tests assert that alongside the skip, because a backoff that quietly
became a penalty would corrupt the exact input the adaptive scheduler ranks on.
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
from orchestrator.models.node import Node
from orchestrator.schedulers.base import NodeSnapshot, passes_hard_filters
from orchestrator.services.leases import sweep_expired_leases
from tests.helpers import (
    register_new_node,
    schedule_single_rank_job,
    send_heartbeat,
)


def _snapshot(node: Node) -> NodeSnapshot:
    return NodeSnapshot(node=node, latest_sample=None, has_active_lease=False)


def _job() -> Job:
    return Job(
        id=uuid.uuid4(),
        spec={"min_gpu_mem_bytes": None},
        scheduler_name="least_loaded",
        state=JobState.QUEUED,
        submitted_by="backoff-test",
    )


def _online(**overrides: object) -> Node:
    now = datetime.now(UTC)
    node = Node(
        id=uuid.uuid4(),
        name="node-backoff",
        public_key="",
        hardware={"gpus": []},
        agent_version="test",
        last_heartbeat_at=now,
    )
    node.status = node.status  # keep the model default (set below explicitly)
    from orchestrator.models.node import NodeStatus

    node.status = NodeStatus.ONLINE
    for key, value in overrides.items():
        setattr(node, key, value)
    return node


# --- The filter itself (pure) -------------------------------------------------


def test_a_backed_off_node_is_filtered_out() -> None:
    now = datetime.now(UTC)
    node = _online(scheduling_backoff_until=now + timedelta(seconds=10))
    assert (
        passes_hard_filters(_job(), _snapshot(node), now=now, stale_seconds=60.0)
        is False
    )


def test_the_backoff_expires_on_its_own() -> None:
    """A timestamp, not a counter: nothing has to remember to clear it, so a
    node that starts claiming again simply becomes eligible."""
    now = datetime.now(UTC)
    node = _online(scheduling_backoff_until=now - timedelta(seconds=1))
    assert (
        passes_hard_filters(_job(), _snapshot(node), now=now, stale_seconds=60.0)
        is True
    )


def test_a_node_that_never_lapsed_an_offer_is_eligible() -> None:
    """NULL means "never lapsed an offer" and must not be read as backed off."""
    now = datetime.now(UTC)
    node = _online(scheduling_backoff_until=None)
    assert (
        passes_hard_filters(_job(), _snapshot(node), now=now, stale_seconds=60.0)
        is True
    )


# --- Wired into the real sweep ------------------------------------------------


@pytest.mark.asyncio
async def test_an_unclaimed_offer_backs_the_node_off_without_blaming_it(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The sweep sets the backoff and leaves reliability alone.

    Both halves matter. Without the backoff the node is re-offered instantly;
    if the backoff became a reliability penalty it would manufacture failures
    for a healthy node, which is the exact bug M7.1a had to repair.
    """
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id = uuid.UUID(str(reg["node_id"]))
    await send_heartbeat(
        api_client, node_id=str(node_id), token=str(reg["access_token"]), gpu_util=5.0
    )

    job = schedule_single_rank_job(session, node_id=node_id, submitted_by="backoff")
    await session.commit()

    # Age the PENDING offer past its TTL so the sweep sees it as overdue.
    await session.execute(
        update(Lease)
        .where(Lease.job_id == job.id)
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=5))
    )
    await session.commit()

    settings = get_settings()
    assert settings.unclaimed_offer_backoff_seconds > 0, "test needs it enabled"
    await sweep_expired_leases(session, settings=settings)
    await session.commit()

    node = (
        await session.execute(
            select(Node).where(Node.id == node_id).execution_options(
                populate_existing=True
            )
        )
    ).scalar_one()

    assert node.scheduling_backoff_until is not None, "the node must be backed off"
    assert node.scheduling_backoff_until > datetime.now(UTC)

    # Blameless: the offer lapsed, nobody dropped anything.
    assert node.lease_failure_count == 0
    assert node.lease_success_count == 0

    lease = (
        await session.execute(select(Lease).where(Lease.job_id == job.id))
    ).scalars().first()
    assert lease is not None
    assert lease.state is LeaseState.UNCLAIMED


@pytest.mark.asyncio
async def test_the_backed_off_node_is_skipped_by_the_scheduler(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """End to end: after the lapse, the very next placement pass must not pick
    that node again. This is the thrash the whole change exists to stop."""
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id = uuid.UUID(str(reg["node_id"]))
    await send_heartbeat(
        api_client, node_id=str(node_id), token=str(reg["access_token"]), gpu_util=5.0
    )

    job = schedule_single_rank_job(session, node_id=node_id, submitted_by="backoff")
    await session.commit()
    await session.execute(
        update(Lease)
        .where(Lease.job_id == job.id)
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=5))
    )
    await session.commit()

    settings = get_settings()
    await sweep_expired_leases(session, settings=settings)
    await session.commit()

    node = (
        await session.execute(
            select(Node).where(Node.id == node_id).execution_options(
                populate_existing=True
            )
        )
    ).scalar_one()

    now = datetime.now(UTC)
    assert (
        passes_hard_filters(_job(), _snapshot(node), now=now, stale_seconds=60.0)
        is False
    ), "the node that just let an offer lapse must not be an immediate candidate"

    # ...and it recovers by itself once the window passes, with no reset call.
    later = now + timedelta(seconds=settings.unclaimed_offer_backoff_seconds + 1)
    assert (
        passes_hard_filters(_job(), _snapshot(node), now=later, stale_seconds=60.0)
        is True
    )
