"""Job API + state-machine tests against real Postgres.

Covers submission/validation, the submit-triggered scheduler pass placing a job
on an eligible node, cancellation (with lease release), rejection of illegal
transitions, and that a JobEvent is written for every transition.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.models.job import Job, JobEvent, JobState
from orchestrator.services.jobs import IllegalTransitionError, transition_job
from tests.helpers import register_new_node, send_heartbeat

_SPEC = {
    "dataset": "cifar10",
    "model": "resnet18",
    "epochs": 2,
    "batch_size": 64,
    "learning_rate": 0.01,
    "world_size": 1,
    "min_gpu_mem_bytes": None,
}


def _submit_body(scheduler_name: str | None = None, **spec_overrides: object) -> dict[str, object]:
    spec = {**_SPEC, **spec_overrides}
    body: dict[str, object] = {"spec": spec}
    if scheduler_name is not None:
        body["scheduler_name"] = scheduler_name
    return body


@pytest.mark.asyncio
async def test_submit_unknown_scheduler_rejected(api_client: AsyncClient) -> None:
    # 'adaptive' is registered as of M3; use a genuinely-unknown name to keep
    # exercising the unknown-scheduler rejection path.
    resp = await api_client.post(
        "/jobs", json=_submit_body(scheduler_name="no_such_scheduler")
    )
    assert resp.status_code == 422
    assert "no_such_scheduler" in resp.text


@pytest.mark.asyncio
async def test_submit_adaptive_scheduler_accepted(api_client: AsyncClient) -> None:
    # 'adaptive' is now a valid registered strategy: submission is accepted
    # (queued when there is no eligible node), not rejected at validation.
    resp = await api_client.post("/jobs", json=_submit_body(scheduler_name="adaptive"))
    assert resp.status_code == 201
    assert resp.json()["scheduler_name"] == "adaptive"


@pytest.mark.asyncio
async def test_submit_with_no_nodes_stays_queued(api_client: AsyncClient) -> None:
    resp = await api_client.post("/jobs", json=_submit_body(scheduler_name="round_robin"))
    assert resp.status_code == 201
    body = resp.json()
    assert body["state"] == "QUEUED"
    assert body["scheduled_node_id"] is None
    # The creation event is present and human-readable.
    assert body["events"][0]["to_state"] == "QUEUED"
    assert "queued" in body["events"][0]["detail"]["message"].lower()


@pytest.mark.asyncio
async def test_submit_places_job_on_eligible_node(api_client: AsyncClient) -> None:
    reg, _key = await register_new_node(api_client, with_gpu=True)
    await send_heartbeat(
        api_client, node_id=reg["node_id"], token=reg["access_token"], gpu_util=5.0
    )

    resp = await api_client.post(
        "/jobs",
        json=_submit_body(scheduler_name="least_loaded", min_gpu_mem_bytes=4 * 1024**3),
    )
    assert resp.status_code == 201
    body = resp.json()
    # The submit-triggered scheduler pass placed it.
    assert body["state"] == "SCHEDULED"
    assert body["scheduled_node_id"] == reg["node_id"]
    states = [e["to_state"] for e in body["events"]]
    assert states == ["QUEUED", "SCHEDULED"]


@pytest.mark.asyncio
async def test_gpu_job_not_placed_on_cpu_only_node(api_client: AsyncClient) -> None:
    reg, _key = await register_new_node(api_client, with_gpu=False)
    await send_heartbeat(api_client, node_id=reg["node_id"], token=reg["access_token"])
    resp = await api_client.post(
        "/jobs",
        json=_submit_body(scheduler_name="least_loaded", min_gpu_mem_bytes=8 * 1024**3),
    )
    assert resp.json()["state"] == "QUEUED"  # no eligible GPU node


@pytest.mark.asyncio
async def test_cancel_queued_job(api_client: AsyncClient) -> None:
    job_id = (await api_client.post("/jobs", json=_submit_body("round_robin"))).json()["id"]
    resp = await api_client.post(f"/jobs/{job_id}/cancel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "CANCELLED"
    assert body["finished_at"] is not None
    assert body["events"][-1]["to_state"] == "CANCELLED"


@pytest.mark.asyncio
async def test_cancel_terminal_job_conflicts(api_client: AsyncClient) -> None:
    job_id = (await api_client.post("/jobs", json=_submit_body("round_robin"))).json()["id"]
    await api_client.post(f"/jobs/{job_id}/cancel")
    again = await api_client.post(f"/jobs/{job_id}/cancel")
    assert again.status_code == 409


@pytest.mark.asyncio
async def test_cancel_unknown_job_404(api_client: AsyncClient) -> None:
    resp = await api_client.post(f"/jobs/{uuid.uuid4()}/cancel")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_illegal_transition_rejected_and_event_written(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    job_id = uuid.UUID(
        (await api_client.post("/jobs", json=_submit_body("round_robin"))).json()["id"]
    )
    job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()

    # Cancel it through the service, then an illegal COMPLETED must be refused
    # and must not mutate the job.
    transition_job(session, job, JobState.CANCELLED, message="cancelled for test")
    await session.commit()

    with pytest.raises(IllegalTransitionError):
        transition_job(session, job, JobState.COMPLETED, message="illegal")
    await session.rollback()

    fresh = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert fresh.state is JobState.CANCELLED

    # Exactly one event per real transition: QUEUED (creation) then CANCELLED.
    events = (
        await session.execute(
            select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id)
        )
    ).scalars().all()
    assert [e.to_state for e in events] == [JobState.QUEUED, JobState.CANCELLED]
    assert all("message" in e.detail for e in events)
