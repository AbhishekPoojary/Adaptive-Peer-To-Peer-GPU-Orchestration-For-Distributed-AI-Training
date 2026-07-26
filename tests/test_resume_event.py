"""The "resumed from checkpoint" timeline event (ADR-006, M6), against a real
Postgres.

When the trainer reports it loaded a checkpoint and continued from a step > 0,
the WebSocket stream turns it into a plain-language JobEvent so the M7 dashboard
timeline reads ``… → reassigned → resumed from checkpoint step N``. This pins
the orchestrator-side service that records it.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.models.job import Job, JobEvent, JobState
from orchestrator.services.jobs import record_resume_event

pytestmark = pytest.mark.asyncio


async def _make_running_job(session: AsyncSession) -> Job:
    job = Job(
        id=uuid.uuid4(),
        spec={"dataset": "mnist", "model": "cnn", "epochs": 3, "batch_size": 32,
              "learning_rate": 0.01, "world_size": 1, "min_gpu_mem_bytes": None},
        scheduler_name="round_robin",
        state=JobState.RUNNING,
        current_lease_epoch=2,
        submitted_by="test",
    )
    session.add(job)
    await session.flush()
    return job


async def test_records_resume_event_without_changing_state(session: AsyncSession) -> None:
    job = await _make_running_job(session)
    await session.commit()

    ok = await record_resume_event(
        session,
        job_id=job.id,
        from_step=1840,
        from_epoch=3,
        checkpoint_key="checkpoints/j/e003-s00001840-abc.pt",
    )
    await session.commit()
    assert ok is True

    events = (
        (
            await session.execute(select(JobEvent).where(JobEvent.job_id == job.id))
        )
        .scalars()
        .all()
    )
    resume_events = [e for e in events if "Resumed from checkpoint" in e.detail["message"]]
    assert len(resume_events) == 1
    ev = resume_events[0]
    # A same-state audit event: the job keeps RUNNING, just gains a timeline entry.
    assert ev.from_state is JobState.RUNNING
    assert ev.to_state is JobState.RUNNING
    assert "step 1840" in ev.detail["message"]
    assert ev.detail["resumed_from_step"] == 1840
    assert ev.detail["resumed_from_epoch"] == 3
    assert ev.detail["checkpoint_key"] == "checkpoints/j/e003-s00001840-abc.pt"
    assert (await session.get(Job, job.id)).state is JobState.RUNNING


async def test_resume_event_on_unknown_job_is_a_noop(session: AsyncSession) -> None:
    ok = await record_resume_event(
        session, job_id=uuid.uuid4(), from_step=10, from_epoch=1
    )
    assert ok is False
