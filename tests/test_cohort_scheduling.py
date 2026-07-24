"""Multi-rank DDP cohort scheduling and fencing (M5, ADR-005 + ADR-003).

The M5 milestone gate, against a real Postgres (the ``session`` fixture — the
concurrency guarantees under test are Postgres row-locking semantics, not
mockable). Covers:

  * top-N cohort selection generalises each baseline scheduler (the N
    least-loaded / N fairest nodes, never a partial cohort);
  * the rendezvous host is the highest-reliability cohort member (ADR-005's R
    term, the Wilson lower bound), whichever scheduler formed the cohort, and it
    is rank 0;
  * the whole cohort shares one lease epoch, and the ADR-003 fence now guards
    the cohort: after a reassignment a stale-epoch rank is rejected while the
    fresh cohort's epoch is accepted;
  * N agents claiming N ranks concurrently grant exactly N leases (one per rank,
    distinct ranks) with zero double-assignment — the M2 race test generalised;
  * one rank failing or timing out tears the whole attempt down (FAILED on an
    explicit failure, REASSIGNED on a timeout), releasing the siblings without
    penalising their nodes.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.config import get_settings
from orchestrator.models.job import Job, JobState
from orchestrator.models.lease import Lease, LeaseState
from orchestrator.models.node import Node
from orchestrator.services.leases import sweep_expired_leases
from orchestrator.services.scheduling import run_scheduler_pass
from tests.helpers import auth_headers, register_new_node, send_heartbeat

_GB = 1024**3


def _cohort_spec(world_size: int, *, min_gpu_mem_bytes: int | None = None) -> dict[str, object]:
    return {
        "dataset": "mnist",
        "model": "small_cnn",
        "epochs": 1,
        "batch_size": 32,
        "learning_rate": 0.01,
        "world_size": world_size,
        "min_gpu_mem_bytes": min_gpu_mem_bytes,
    }


async def _online_node(
    api_client: AsyncClient, *, gpu_util: float
) -> tuple[str, str]:
    """Register + heartbeat one GPU node so it is ONLINE with real telemetry.
    Returns (node_id, token)."""
    reg, _key = await register_new_node(api_client, with_gpu=True)
    await send_heartbeat(
        api_client, node_id=reg["node_id"], token=reg["access_token"], gpu_util=gpu_util
    )
    return reg["node_id"], reg["access_token"]


def _queue_job(session: AsyncSession, *, world_size: int, scheduler_name: str) -> Job:
    job = Job(
        id=uuid.uuid4(),
        spec=_cohort_spec(world_size),
        scheduler_name=scheduler_name,
        state=JobState.QUEUED,
        submitted_by="cohort-test",
    )
    session.add(job)
    return job


def _add_terminal_lease(
    session: AsyncSession, *, node_id: uuid.UUID, state: LeaseState, now: datetime
) -> None:
    """Insert one terminal lease (+ its terminal job) as reliability history for
    ``node_id``. COMPLETED counts as a success, FAILED/EXPIRED as a failure."""
    job_state = JobState.COMPLETED if state is LeaseState.COMPLETED else JobState.FAILED
    job = Job(
        id=uuid.uuid4(),
        spec=_cohort_spec(1),
        scheduler_name="round_robin",
        state=job_state,
        submitted_by="history",
    )
    session.add(job)
    session.add(
        Lease(
            id=uuid.uuid4(),
            job_id=job.id,
            node_id=node_id,
            lease_epoch=1,
            rank=0,
            state=state,
            granted_at=now - timedelta(seconds=30),
            expires_at=now,
            released_at=None if state is LeaseState.EXPIRED else now,
        )
    )


async def _pending_leases(session: AsyncSession, job_id: uuid.UUID) -> list[Lease]:
    return list(
        (
            await session.execute(
                select(Lease)
                .where(Lease.job_id == job_id, Lease.state == LeaseState.PENDING)
                .order_by(Lease.rank)
            )
        ).scalars().all()
    )


async def _lease(session: AsyncSession, lease_id: str) -> Lease:
    return (
        await session.execute(select(Lease).where(Lease.id == uuid.UUID(lease_id)))
    ).scalar_one()


async def _node(session: AsyncSession, node_id: str) -> Node:
    node = (
        await session.execute(select(Node).where(Node.id == uuid.UUID(node_id)))
    ).scalar_one()
    await session.refresh(node)
    return node


# --- Top-N selection ---------------------------------------------------------


@pytest.mark.asyncio
async def test_least_loaded_selects_top_n_least_loaded_as_cohort(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    settings = get_settings()
    # Three nodes with distinct GPU load; a world_size=2 job must take the two
    # LEAST loaded and leave the busiest out — a real top-N generalisation.
    idle, _ = await _online_node(api_client, gpu_util=5.0)
    mid, _ = await _online_node(api_client, gpu_util=45.0)
    busy, _ = await _online_node(api_client, gpu_util=95.0)
    job = _queue_job(session, world_size=2, scheduler_name="least_loaded")
    await session.commit()

    placed = await run_scheduler_pass(session, settings=settings)
    await session.commit()
    assert placed == 1

    leases = await _pending_leases(session, job.id)
    chosen = {str(lease.node_id) for lease in leases}
    assert len(leases) == 2, "a world_size=2 cohort must be exactly two leases"
    assert chosen == {idle, mid}, f"expected the two least-loaded, got {chosen}"
    assert busy not in chosen


@pytest.mark.asyncio
async def test_adaptive_selects_top_n_and_audits_whole_cohort(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    from orchestrator.services.scheduling import list_scheduling_decisions

    settings = get_settings()
    # Three nodes, distinct load; adaptive world_size=2 must take the two lowest
    # S_i and audit-log the decision with BOTH marked selected (generalising the
    # M3 single-winner audit to a cohort).
    idle, _ = await _online_node(api_client, gpu_util=5.0)
    mid, _ = await _online_node(api_client, gpu_util=45.0)
    busy, _ = await _online_node(api_client, gpu_util=95.0)
    job = _queue_job(session, world_size=2, scheduler_name="adaptive")
    await session.commit()

    placed = await run_scheduler_pass(session, settings=settings)
    await session.commit()
    assert placed == 1

    chosen = {str(lease.node_id) for lease in await _pending_leases(session, job.id)}
    assert chosen == {idle, mid}, f"adaptive cohort should be the two lowest-load, got {chosen}"

    decisions = await list_scheduling_decisions(session, job_id=job.id)
    assert len(decisions) == 1
    selected = {str(c.node_id) for c in decisions[0].candidates if c.was_selected}
    assert selected == {idle, mid}, "the whole cohort is marked selected in the audit"
    assert busy not in selected


@pytest.mark.asyncio
async def test_partial_cohort_is_not_placed(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    settings = get_settings()
    # Only one eligible node but world_size=2: no partial cohort is ever placed.
    await _online_node(api_client, gpu_util=5.0)
    job = _queue_job(session, world_size=2, scheduler_name="least_loaded")
    await session.commit()

    placed = await run_scheduler_pass(session, settings=settings)
    await session.commit()
    assert placed == 0

    fresh = (await session.execute(select(Job).where(Job.id == job.id))).scalar_one()
    await session.refresh(fresh)
    assert fresh.state is JobState.QUEUED
    assert await _pending_leases(session, job.id) == []


# --- Rendezvous host = highest reliability -----------------------------------


@pytest.mark.asyncio
async def test_rendezvous_host_is_highest_reliability_member(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    # Exactly two nodes so both are selected regardless of scheduler; they differ
    # only in reliability history. The rendezvous host must be the more reliable
    # one (ADR-005) and must be rank 0.
    reliable, _ = await _online_node(api_client, gpu_util=40.0)
    flaky, _ = await _online_node(api_client, gpu_util=40.0)
    reliable_id, flaky_id = uuid.UUID(reliable), uuid.UUID(flaky)
    for _ in range(5):
        _add_terminal_lease(session, node_id=reliable_id, state=LeaseState.COMPLETED, now=now)
    for _ in range(5):
        _add_terminal_lease(session, node_id=flaky_id, state=LeaseState.FAILED, now=now)
    job = _queue_job(session, world_size=2, scheduler_name="least_loaded")
    await session.commit()

    await run_scheduler_pass(session, settings=settings)
    await session.commit()

    fresh = (await session.execute(select(Job).where(Job.id == job.id))).scalar_one()
    await session.refresh(fresh)
    assert fresh.rendezvous_node_id == reliable_id
    assert fresh.scheduled_node_id == reliable_id  # rank-0 node, for read continuity

    leases = await _pending_leases(session, job.id)
    by_rank = {lease.rank: lease.node_id for lease in leases}
    assert by_rank[0] == reliable_id, "the rendezvous host takes rank 0"
    assert by_rank[1] == flaky_id


# --- Cohort claim flow (multi-rank, distinct ranks) --------------------------


@pytest.mark.asyncio
async def test_two_agents_claim_two_distinct_ranks(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    settings = get_settings()
    node_a, token_a = await _online_node(api_client, gpu_util=10.0)
    node_b, token_b = await _online_node(api_client, gpu_util=20.0)
    job = _queue_job(session, world_size=2, scheduler_name="least_loaded")
    await session.commit()
    await run_scheduler_pass(session, settings=settings)
    await session.commit()

    claim_a = (
        await api_client.post(f"/nodes/{node_a}/leases/claim", headers=auth_headers(token_a))
    ).json()
    claim_b = (
        await api_client.post(f"/nodes/{node_b}/leases/claim", headers=auth_headers(token_b))
    ).json()

    assert claim_a["lease"] is not None and claim_b["lease"] is not None
    # Same attempt epoch, distinct ranks, correct world_size in the rendezvous info.
    assert claim_a["lease"]["lease_epoch"] == claim_b["lease"]["lease_epoch"] == 1
    ranks = {claim_a["lease"]["rank"], claim_b["lease"]["rank"]}
    assert ranks == {0, 1}, f"cohort ranks must be 0 and 1, got {ranks}"
    for claim in (claim_a, claim_b):
        rdzv = claim["rendezvous"]
        assert rdzv["world_size"] == 2
        assert rdzv["backend"] == settings.training_backend
        assert rdzv["endpoint"].endswith(f":{settings.rendezvous_port}")
        assert rdzv["rdzv_id"].endswith("-1")  # attempt epoch 1
    # Exactly one rank is the rendezvous host.
    is_host = [c["rendezvous"]["is_rendezvous_host"] for c in (claim_a, claim_b)]
    assert is_host.count(True) == 1

    # DB truth: two ACTIVE leases on the one job, distinct ranks, job LEASED.
    actives = (
        await session.execute(
            select(Lease).where(Lease.job_id == job.id, Lease.state == LeaseState.ACTIVE)
        )
    ).scalars().all()
    assert len(actives) == 2
    assert {lease.rank for lease in actives} == {0, 1}
    fresh = (await session.execute(select(Job).where(Job.id == job.id))).scalar_one()
    await session.refresh(fresh)
    assert fresh.state is JobState.LEASED


@pytest.mark.asyncio
async def test_concurrent_cohort_claims_grant_exactly_n_no_double(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The M2 race test generalised to a cohort: each rank's node fires several
    concurrent claims; exactly N leases are granted (one ACTIVE per rank), the
    rest come back empty, and no rank is ever double-assigned."""
    settings = get_settings()
    nodes = [await _online_node(api_client, gpu_util=10.0 + i) for i in range(3)]
    job = _queue_job(session, world_size=3, scheduler_name="least_loaded")
    await session.commit()
    await run_scheduler_pass(session, settings=settings)
    await session.commit()

    async def claim(node_id: str, token: str) -> bool:
        resp = await api_client.post(
            f"/nodes/{node_id}/leases/claim", headers=auth_headers(token)
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["lease"] is not None

    # 5 concurrent claims per node, all fired at once (3 x 5 = 15 attempts).
    tasks = [claim(node_id, token) for node_id, token in nodes for _ in range(5)]
    results = await asyncio.gather(*tasks)

    granted = sum(1 for r in results if r)
    assert granted == 3, f"expected exactly 3 grants (one per rank), got {granted}"

    actives = (
        await session.execute(
            select(Lease).where(Lease.job_id == job.id, Lease.state == LeaseState.ACTIVE)
        )
    ).scalars().all()
    assert len(actives) == 3
    assert {lease.rank for lease in actives} == {0, 1, 2}, "each rank claimed exactly once"

    # No (job, rank) has two non-terminal leases.
    dupes = (
        await session.execute(
            select(Lease.rank, func.count())
            .where(Lease.job_id == job.id, Lease.state == LeaseState.ACTIVE)
            .group_by(Lease.rank)
            .having(func.count() > 1)
        )
    ).all()
    assert dupes == []


# --- Cohort epoch fencing ----------------------------------------------------


@pytest.mark.asyncio
async def test_cohort_epoch_fencing_across_reassignment(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """A whole cohort shares one epoch; after a timeout reassigns the attempt, a
    stale-epoch rank is fenced out (409) while the fresh cohort's new epoch is
    accepted for every rank."""
    settings = get_settings()
    node_a, token_a = await _online_node(api_client, gpu_util=10.0)
    node_b, token_b = await _online_node(api_client, gpu_util=20.0)
    job = _queue_job(session, world_size=2, scheduler_name="least_loaded")
    await session.commit()
    await run_scheduler_pass(session, settings=settings)
    await session.commit()

    lease_a = (
        await api_client.post(f"/nodes/{node_a}/leases/claim", headers=auth_headers(token_a))
    ).json()["lease"]
    lease_b = (
        await api_client.post(f"/nodes/{node_b}/leases/claim", headers=auth_headers(token_b))
    ).json()["lease"]
    assert lease_a["lease_epoch"] == lease_b["lease_epoch"] == 1

    # Time out node A's rank; the sweep must tear down the WHOLE cohort.
    await session.execute(
        update(Lease)
        .where(Lease.id == uuid.UUID(lease_a["id"]))
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()
    swept = await sweep_expired_leases(session, settings=settings)
    await session.commit()
    assert swept == 1  # one lease actually timed out

    job_row = (await session.execute(select(Job).where(Job.id == job.id))).scalar_one()
    await session.refresh(job_row)
    assert job_row.state is JobState.REASSIGNED
    # Node A's lease EXPIRED; node B's sibling RELEASED (not its fault, no penalty).
    la = await _lease(session, lease_a["id"])
    lb = await _lease(session, lease_b["id"])
    assert la.state is LeaseState.EXPIRED
    assert lb.state is LeaseState.RELEASED
    node_b_row = await _node(session, node_b)
    assert node_b_row.lease_failure_count == 0, "the innocent sibling node is not penalised"

    # A stale-epoch (1) write from node B's old lease is fenced out.
    stale = await api_client.post(
        f"/leases/{lease_b['id']}/renew",
        json={"lease_epoch": 1},
        headers=auth_headers(token_b),
    )
    assert stale.status_code == 409

    # Re-place a fresh cohort at epoch 2 and confirm both ranks accept it.
    placed = await run_scheduler_pass(session, settings=settings)
    await session.commit()
    assert placed == 1
    new_a = (
        await api_client.post(f"/nodes/{node_a}/leases/claim", headers=auth_headers(token_a))
    ).json()["lease"]
    new_b = (
        await api_client.post(f"/nodes/{node_b}/leases/claim", headers=auth_headers(token_b))
    ).json()["lease"]
    assert new_a["lease_epoch"] == new_b["lease_epoch"] == 2
    for lease, token in ((new_a, token_a), (new_b, token_b)):
        ok = await api_client.post(
            f"/leases/{lease['id']}/renew",
            json={"lease_epoch": 2},
            headers=auth_headers(token),
        )
        assert ok.status_code == 200


# --- Cohort failure teardown -------------------------------------------------


@pytest.mark.asyncio
async def test_one_rank_failure_fails_whole_attempt(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    settings = get_settings()
    node_a, token_a = await _online_node(api_client, gpu_util=10.0)
    node_b, token_b = await _online_node(api_client, gpu_util=20.0)
    job = _queue_job(session, world_size=2, scheduler_name="least_loaded")
    await session.commit()
    await run_scheduler_pass(session, settings=settings)
    await session.commit()

    lease_a = (
        await api_client.post(f"/nodes/{node_a}/leases/claim", headers=auth_headers(token_a))
    ).json()["lease"]
    lease_b = (
        await api_client.post(f"/nodes/{node_b}/leases/claim", headers=auth_headers(token_b))
    ).json()["lease"]

    # Node A reports a real training failure → the whole attempt is TERMINAL.
    failed = await api_client.post(
        f"/leases/{lease_a['id']}/fail",
        json={"lease_epoch": 1, "reason": "trainer exited with code 1"},
        headers=auth_headers(token_a),
    )
    assert failed.status_code == 200

    job_row = (await session.execute(select(Job).where(Job.id == job.id))).scalar_one()
    await session.refresh(job_row)
    assert job_row.state is JobState.FAILED

    la = await _lease(session, lease_a["id"])
    lb = await _lease(session, lease_b["id"])
    assert la.state is LeaseState.FAILED
    assert lb.state is LeaseState.RELEASED  # sibling torn down, not failed

    # Only the failing node earns a reliability failure.
    node_a_row = await _node(session, node_a)
    node_b_row = await _node(session, node_b)
    assert node_a_row.lease_failure_count == 1
    assert node_b_row.lease_failure_count == 0

    # Node B's now-released lease can no longer be completed (409, fenced).
    late = await api_client.post(
        f"/leases/{lease_b['id']}/complete",
        json={"lease_epoch": 1},
        headers=auth_headers(token_b),
    )
    assert late.status_code == 409


@pytest.mark.asyncio
async def test_both_ranks_complete_marks_job_completed(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    settings = get_settings()
    node_a, token_a = await _online_node(api_client, gpu_util=10.0)
    node_b, token_b = await _online_node(api_client, gpu_util=20.0)
    job = _queue_job(session, world_size=2, scheduler_name="least_loaded")
    await session.commit()
    await run_scheduler_pass(session, settings=settings)
    await session.commit()

    claim_a = (
        await api_client.post(f"/nodes/{node_a}/leases/claim", headers=auth_headers(token_a))
    ).json()
    claim_b = (
        await api_client.post(f"/nodes/{node_b}/leases/claim", headers=auth_headers(token_b))
    ).json()

    # The rank-0 (rendezvous host) agent reports the real training result; the
    # other rank completes silently (result omitted).
    host = claim_a if claim_a["rendezvous"]["is_rendezvous_host"] else claim_b
    host_token = token_a if host is claim_a else token_b
    worker = claim_b if host is claim_a else claim_a
    worker_token = token_b if host is claim_a else token_a

    r_worker = await api_client.post(
        f"/leases/{worker['lease']['id']}/complete",
        json={"lease_epoch": 1},
        headers=auth_headers(worker_token),
    )
    assert r_worker.status_code == 200
    r_host = await api_client.post(
        f"/leases/{host['lease']['id']}/complete",
        json={
            "lease_epoch": 1,
            "result": {
                "final_loss": 0.12,
                "final_test_accuracy": 0.97,
                "epochs_completed": 1,
                "exit_code": 0,
                "device": "cpu",
            },
        },
        headers=auth_headers(host_token),
    )
    assert r_host.status_code == 200

    job_row = (await session.execute(select(Job).where(Job.id == job.id))).scalar_one()
    await session.refresh(job_row)
    assert job_row.state is JobState.COMPLETED
    # The rank-0 result is the one persisted, never clobbered by the worker's null.
    assert job_row.result is not None
    assert job_row.result["final_test_accuracy"] == 0.97

    # Both nodes earned a success (both really did the work).
    for node_id in (node_a, node_b):
        n = (await session.execute(select(Node).where(Node.id == uuid.UUID(node_id)))).scalar_one()
        await session.refresh(n)
        assert n.lease_success_count == 1
