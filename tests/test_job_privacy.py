"""A job's logs, metrics and model belong to whoever submitted it.

Any signed-in user could read another's job logs (which name their dataset's
classes), metrics and trained model -- including the person hosting the
orchestrator. services/ownership.py now decides; these pin each route against
three people: the submitter, another operator, and an admin.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.models.user import UserRole
from orchestrator.services import checkpoints as checkpoints_service
from orchestrator.services.object_store import ObjectNotFoundError
from tests.helpers import auth_headers, seed_user, user_token

_SPEC = {
    "dataset": "mnist",
    "model": "small_cnn",
    "epochs": 1,
    "batch_size": 8,
    "learning_rate": 0.01,
    "world_size": 1,
    "min_gpu_mem_bytes": None,
}

_CONTENTS = ("logs", "metrics", "checkpoint", "checkpoint/download")


class StubEmptyCheckpointStore:
    """No checkpoint saved for any job (stands in for MinIO)."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def get_bytes(self, *, key: str) -> bytes:
        raise ObjectNotFoundError(key)


@pytest.fixture(autouse=True)
def empty_checkpoint_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkpoints_service, "CheckpointObjectStore", StubEmptyCheckpointStore)


async def _submit(api_client: AsyncClient) -> str:
    response = await api_client.post("/jobs", json={"spec": _SPEC})  # as the operator
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


@pytest.mark.asyncio
async def test_another_operator_cannot_see_the_job_at_all(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    job_id = await _submit(api_client)
    other = await seed_user(session, username="someone-else", role=UserRole.OPERATOR)
    them = auth_headers(user_token(other))

    listing = await api_client.get("/jobs", headers=them)
    assert all(j["id"] != job_id for j in listing.json()["jobs"])
    for path in ("", "/scheduling-decisions", *(f"/{c}" for c in _CONTENTS)):
        response = await api_client.get(f"/jobs/{job_id}{path}", headers=them)
        assert response.status_code == 404, (path, response.status_code)
    cancel = await api_client.post(f"/jobs/{job_id}/cancel", headers=them)
    assert cancel.status_code == 404


@pytest.mark.asyncio
async def test_an_admin_sees_the_job_but_not_its_contents(api_client: AsyncClient) -> None:
    job_id = await _submit(api_client)
    admin = auth_headers(api_client.admin_token)  # type: ignore[attr-defined]

    listing = await api_client.get("/jobs", headers=admin)
    assert any(j["id"] == job_id for j in listing.json()["jobs"])
    detail = await api_client.get(f"/jobs/{job_id}", headers=admin)
    assert detail.status_code == 200
    assert detail.json()["contents_visible"] is False
    assert detail.json()["result"] is None
    for content in _CONTENTS:
        response = await api_client.get(f"/jobs/{job_id}/{content}", headers=admin)
        assert response.status_code == 403, (content, response.status_code)
        assert "only the person who submitted" in response.json()["detail"]
    # Fleet management still works: an admin may stop a job.
    cancel = await api_client.post(f"/jobs/{job_id}/cancel", headers=admin)
    assert cancel.status_code == 200


@pytest.mark.asyncio
async def test_the_submitter_sees_everything(api_client: AsyncClient) -> None:
    job_id = await _submit(api_client)
    detail = await api_client.get(f"/jobs/{job_id}")
    assert detail.json()["contents_visible"] is True
    assert (await api_client.get(f"/jobs/{job_id}/logs")).status_code == 200
    assert (await api_client.get(f"/jobs/{job_id}/metrics")).status_code == 200
    # No model yet: 404 means "none saved", not "not yours" (that would be 403).
    assert (await api_client.get(f"/jobs/{job_id}/checkpoint")).status_code == 404
    listing = await api_client.get("/jobs")
    assert [j["id"] for j in listing.json()["jobs"]] == [job_id]
