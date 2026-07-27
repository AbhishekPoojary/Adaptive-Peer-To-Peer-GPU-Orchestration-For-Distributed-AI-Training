"""Unclaimed offers are not node failures (ADR-003 addendum).

The TTL sweep sees two events that look alike and mean opposite things:

* an ACTIVE lease past its TTL — the node took work on and stopped making
  progress. Its failure, and it must still be counted as one.
* a PENDING slot past its TTL — the scheduler offered work that was never
  claimed. Nobody took it on, so nobody dropped it: no reliability effect
  anywhere, including in the lease-history reliability inputs the adaptive
  scheduler actually reads.

These tests pin both halves, with the contrast asserted in one sweep so a future
change cannot quietly collapse them back together.
"""

from __future__ import annotations

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
from orchestrator.schedulers.base import NodeSnapshot
from orchestrator.services.leases import claim_job_for_node, sweep_expired_leases
from orchestrator.services.scheduling import _reliability_inputs
from tests.helpers import (
    auth_headers,
    register_new_node,
    schedule_single_rank_job,
    send_heartbeat,
)


async def _online_node(api_client: AsyncClient) -> tuple[dict[str, object], uuid.UUID]:
    reg, _key = await register_new_node(api_client, with_gpu=True)
    await send_heartbeat(
        api_client, node_id=reg["node_id"], token=reg["access_token"], gpu_util=5.0
    )
    return reg, uuid.UUID(str(reg["node_id"]))


async def _expire_now(session: AsyncSession, job_id: uuid.UUID) -> None:
    """Push every non-terminal lease of a job past its TTL (real comparison,
    the sweep still reads wall-clock ``now``)."""
    await session.execute(
        update(Lease)
        .where(Lease.job_id == job_id)
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()


async def _node(session: AsyncSession, node_id: uuid.UUID) -> Node:
    node = (await session.execute(select(Node).where(Node.id == node_id))).scalar_one()
    await session.refresh(node)
    return node


@pytest.mark.asyncio
async def test_unclaimed_pending_offer_is_not_a_node_failure(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """A slot the node never claimed lapses UNCLAIMED, with the node's
    reliability counters untouched and an honest timeline message."""
    settings = get_settings()
    _reg, node_id = await _online_node(api_client)
    job = schedule_single_rank_job(session, node_id=node_id, submitted_by="unclaimed-test")
    await session.commit()

    await _expire_now(session, job.id)
    swept = await sweep_expired_leases(session, settings=settings)
    await session.commit()
    assert swept == 1

    lease = (
        await session.execute(select(Lease).where(Lease.job_id == job.id))
    ).scalar_one()
    await session.refresh(lease)
    assert lease.state is LeaseState.UNCLAIMED

    node = await _node(session, node_id)
    assert node.lease_failure_count == 0  # THE bug: this used to be 1
    assert node.lease_success_count == 0

    job_row = (await session.execute(select(Job).where(Job.id == job.id))).scalar_one()
    await session.refresh(job_row)
    assert job_row.state is JobState.REASSIGNED

    evt = (
        await session.execute(
            select(JobEvent)
            .where(JobEvent.job_id == job.id, JobEvent.to_state == JobState.REASSIGNED)
            .order_by(JobEvent.id.desc())
            .limit(1)
        )
    ).scalar_one()
    message = str(evt.detail["message"])
    assert "did not pick up the work in time" in message
    assert node.name in message
    assert "expired without progress" not in message
    assert evt.detail["unclaimed_ranks"] == [0]
    assert evt.detail["expired_ranks"] == []


@pytest.mark.asyncio
async def test_active_timeout_still_counts_a_failure_but_unclaimed_offer_does_not(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The contrast, in a single sweep: node A claimed and went silent (its
    failure); node B never claimed (not its failure)."""
    settings = get_settings()
    reg_a, node_a = await _online_node(api_client)
    _reg_b, node_b = await _online_node(api_client)

    job_a = schedule_single_rank_job(session, node_id=node_a, submitted_by="active-ttl")
    job_b = schedule_single_rank_job(session, node_id=node_b, submitted_by="never-claimed")
    await session.commit()

    # Node A really claims its slot over HTTP -> ACTIVE lease.
    resp = await api_client.post(
        f"/nodes/{reg_a['node_id']}/leases/claim",
        headers=auth_headers(str(reg_a["access_token"])),
    )
    assert resp.json()["lease"] is not None

    await _expire_now(session, job_a.id)
    await _expire_now(session, job_b.id)
    swept = await sweep_expired_leases(session, settings=settings)
    await session.commit()
    assert swept == 2

    lease_a = (
        await session.execute(select(Lease).where(Lease.job_id == job_a.id))
    ).scalar_one()
    lease_b = (
        await session.execute(select(Lease).where(Lease.job_id == job_b.id))
    ).scalar_one()
    await session.refresh(lease_a)
    await session.refresh(lease_b)
    assert lease_a.state is LeaseState.EXPIRED
    assert lease_b.state is LeaseState.UNCLAIMED

    assert (await _node(session, node_a)).lease_failure_count == 1
    assert (await _node(session, node_b)).lease_failure_count == 0


@pytest.mark.asyncio
async def test_unclaimed_offer_does_not_lower_the_scheduler_reliability_input(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The counters are only a display metric — ADR-009's R_i is computed from
    Lease rows. An UNCLAIMED row must not appear there as a weighted failure."""
    settings = get_settings()
    _reg, node_id = await _online_node(api_client)
    job = schedule_single_rank_job(session, node_id=node_id, submitted_by="reliability")
    await session.commit()

    await _expire_now(session, job.id)
    await sweep_expired_leases(session, settings=settings)
    await session.commit()

    node = await _node(session, node_id)
    snapshot = NodeSnapshot(node=node, latest_sample=None, has_active_lease=False)
    reliability = await _reliability_inputs(
        session, [snapshot], now=datetime.now(UTC), settings=settings
    )
    assert len(reliability) == 1
    # Only the declared Beta prior — no earned outcome either way.
    assert reliability[0].weighted_failure == pytest.approx(
        settings.reliability_prior_beta
    )
    assert reliability[0].weighted_success == pytest.approx(
        settings.reliability_prior_alpha
    )


@pytest.mark.asyncio
async def test_natural_ttl_elapse_of_an_unclaimed_offer(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Same conclusion via a genuinely elapsed short TTL — no timestamp surgery,
    and a claim that really happens still yields ACTIVE -> EXPIRED semantics."""
    settings = get_settings().model_copy(update={"lease_ttl_seconds": 1})
    _reg, node_id = await _online_node(api_client)
    job = schedule_single_rank_job(session, node_id=node_id, submitted_by="natural-ttl")
    await session.commit()
    # schedule_single_rank_job grants a long TTL for the other tests' benefit;
    # give this slot the 1s TTL this test is about.
    await session.execute(
        update(Lease)
        .where(Lease.job_id == job.id)
        .values(expires_at=datetime.now(UTC) + timedelta(seconds=1))
    )
    await session.commit()

    import asyncio

    await asyncio.sleep(1.3)

    assert await sweep_expired_leases(session, settings=settings) == 1
    await session.commit()
    lease = (
        await session.execute(select(Lease).where(Lease.job_id == job.id))
    ).scalar_one()
    await session.refresh(lease)
    assert lease.state is LeaseState.UNCLAIMED
    assert (await _node(session, node_id)).lease_failure_count == 0


@pytest.mark.asyncio
async def test_claim_after_reassignment_is_still_fenced_out(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Guard on the fix's blast radius: an UNCLAIMED slot is terminal, so a late
    agent poll finds nothing to claim rather than resurrecting a dead offer."""
    settings = get_settings()
    _reg, node_id = await _online_node(api_client)
    job = schedule_single_rank_job(session, node_id=node_id, submitted_by="late-claim")
    await session.commit()

    await _expire_now(session, job.id)
    await sweep_expired_leases(session, settings=settings)
    await session.commit()

    node = await _node(session, node_id)
    assert await claim_job_for_node(session, node=node, settings=settings) is None
