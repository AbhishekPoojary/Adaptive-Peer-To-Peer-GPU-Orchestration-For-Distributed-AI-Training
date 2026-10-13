"""Minimum-completion-time placement: wait for a fast busy node when it pays.

On the real fleet a 12-job queue took 235 s on one GPU laptop but 340 s once
a CPU laptop joined, because the CPU took 100 s jobs the GPU would have
finished in 20. These tests rebuild that situation in the database: GPU busy,
CPU idle, run times measured from past attempts.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.models.job import Job, JobState
from orchestrator.models.lease import Lease, LeaseState
from orchestrator.services.scheduling import run_scheduler_pass
from tests.test_adaptive_scheduler import _add_sample, _make_node, _settings

_SPEC = {"dataset": "cifar10", "model": "small_cnn", "epochs": 1, "batch_size": 128,
         "learning_rate": 0.001, "world_size": 1, "min_gpu_mem_bytes": None}


def _lease(session: AsyncSession, node, *, state: LeaseState, granted: datetime,  # type: ignore[no-untyped-def]
           released: datetime | None) -> None:
    job = Job(id=uuid.uuid4(), spec=dict(_SPEC), scheduler_name="adaptive",
              state=JobState.COMPLETED if state is LeaseState.COMPLETED else JobState.RUNNING,
              submitted_by="history")
    session.add(job)
    session.add(Lease(id=uuid.uuid4(), job_id=job.id, node_id=node.id, lease_epoch=1,
                      state=state, granted_at=granted, expires_at=granted + timedelta(hours=1),
                      released_at=released))


async def _fleet(
    session: AsyncSession, scheduler: str, queued: int, busy_for: float = 0.0
) -> list[Job]:
    now = datetime.now(UTC)
    gpu = _make_node(session, "gpu", with_gpu=True, now=now)
    cpu = _make_node(session, "cpu", with_gpu=False, now=now)
    for node in (gpu, cpu):
        _add_sample(session, node, gpu_util=None, rtt_ewma=20.0, cpu=5.0, now=now)
    # Measured history: the GPU runs this kind of job in 20 s, the CPU in 100 s.
    past = now - timedelta(hours=1)
    for node, seconds in ((gpu, 20), (cpu, 100)):
        _lease(session, node, state=LeaseState.COMPLETED, granted=past,
               released=past + timedelta(seconds=seconds))
    # The GPU is busy with a job that started just now.
    _lease(session, gpu, state=LeaseState.ACTIVE, granted=now - timedelta(seconds=busy_for),
           released=None)
    jobs = [Job(id=uuid.uuid4(), spec=dict(_SPEC), scheduler_name=scheduler,
                state=JobState.QUEUED, submitted_by="queue",
                submitted_at=now + timedelta(milliseconds=i)) for i in range(queued)]
    session.add_all(jobs)
    await session.commit()
    return jobs


async def _states(session: AsyncSession, jobs: list[Job]) -> list[str]:
    ids = [j.id for j in jobs]
    rows = (await session.execute(select(Job.id, Job.state).where(Job.id.in_(ids)))).all()
    by_id = dict(rows)
    return [by_id[i].value for i in ids]


@pytest.mark.asyncio
async def test_a_short_queue_waits_for_the_fast_busy_node(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    jobs = await _fleet(session, "adaptive", queued=2)
    await run_scheduler_pass(session, settings=_settings())
    await session.commit()
    # 20 s to free + 20 s each beats 100 s on the idle CPU, for both jobs.
    assert await _states(session, jobs) == ["QUEUED", "QUEUED"]


@pytest.mark.asyncio
async def test_a_long_queue_spills_onto_the_slow_node(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    jobs = await _fleet(session, "adaptive", queued=6)
    await run_scheduler_pass(session, settings=_settings())
    await session.commit()
    states = await _states(session, jobs)
    # Waiting stops paying once the GPU's queue reaches 100 s: the CPU then helps.
    assert states.count("SCHEDULED") == 1
    assert states[:4] == ["QUEUED"] * 4


@pytest.mark.asyncio
async def test_the_greedy_ablation_takes_the_idle_slow_node(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    jobs = await _fleet(session, "adaptive_no_defer", queued=1)
    await run_scheduler_pass(session, settings=_settings())
    await session.commit()
    assert await _states(session, jobs) == ["SCHEDULED"]


@pytest.mark.asyncio
async def test_an_overrunning_job_stops_holding_the_queue(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The GPU's current job should take 20 s but has run 150 s. It is not
    about to finish: assume 130 s more, so the idle CPU (100 s) is better."""
    jobs = await _fleet(session, "adaptive", queued=1, busy_for=150.0)
    await run_scheduler_pass(session, settings=_settings())
    await session.commit()
    assert await _states(session, jobs) == ["SCHEDULED"]
