"""Retrieving a job's trained model (ADR-006 addendum 2).

``StubCheckpointStore`` stands in for MinIO for the same reason
``test_datasets.py`` stubs it: nothing asserted here depends on S3 behaviour,
only on which key the orchestrator resolves and what it does when storage
answers "missing" versus "unreachable". Requiring a running MinIO to test a
manifest lookup would buy no extra confidence.

The manifests written below are real ones — built by ``trainer.checkpoint``'s
own writer where possible — so a change to the manifest format breaks these
tests rather than silently breaking downloads.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from orchestrator.services import checkpoints as checkpoints_service
from orchestrator.services.object_store import ObjectNotFoundError, ObjectStoreError
from trainer.checkpoint import manifest_key

_SPEC: dict[str, Any] = {
    "dataset": "cifar10",
    "model": "small_cnn",
    "epochs": 1,
    "batch_size": 8,
    "learning_rate": 0.01,
    "world_size": 1,
    "min_gpu_mem_bytes": None,
}


class StubCheckpointStore:
    """In-memory stand-in for the checkpoints bucket."""

    objects: dict[str, bytes] = {}
    sizes: dict[str, int | None] = {}
    streamed: list[str] = []
    unreachable = False

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def get_bytes(self, *, key: str) -> bytes:
        if type(self).unreachable:
            raise ObjectStoreError("stubbed storage outage")
        try:
            return type(self).objects[key]
        except KeyError as exc:
            raise ObjectNotFoundError(key) from exc

    def head_size_bytes(self, *, key: str) -> int | None:
        return type(self).sizes.get(key)

    def iter_object(self, *, key: str, chunk_bytes: int = 1024 * 1024):  # type: ignore[no-untyped-def]
        type(self).streamed.append(key)
        if type(self).unreachable:
            raise ObjectStoreError("stubbed storage outage")
        blob = type(self).objects.get(key)
        if blob is None:
            raise ObjectNotFoundError(key)
        # Deliberately more than one chunk, so the streaming path is exercised
        # rather than a single yield that would hide a reassembly bug.
        for i in range(0, len(blob), 8):
            yield blob[i : i + 8]


@pytest.fixture(autouse=True)
def stub_checkpoint_store(monkeypatch: pytest.MonkeyPatch) -> None:
    StubCheckpointStore.objects = {}
    StubCheckpointStore.sizes = {}
    StubCheckpointStore.streamed = []
    StubCheckpointStore.unreachable = False
    monkeypatch.setattr(
        checkpoints_service, "CheckpointObjectStore", StubCheckpointStore
    )


def put_manifest(job_id: str, latest: dict[str, Any] | None) -> None:
    """Write a manifest for ``job_id`` exactly where the trainer would."""
    body: dict[str, Any] = {"schema_version": 1, "job_id": job_id}
    if latest is not None:
        body["latest"] = latest
    StubCheckpointStore.objects[manifest_key(job_id)] = json.dumps(body).encode()


def sample_entry(job_id: str) -> dict[str, Any]:
    return {
        "key": f"checkpoints/{job_id}/e002-s00000500-abc12345.pt",
        "step": 500,
        "epoch": 2,
        "world_size": 1,
        "lease_epoch": 1,
        "timestamp_utc": "2026-08-28T10:00:00Z",
        "loss": 0.1234,
    }


async def submit_job(client: AsyncClient) -> str:
    response = await client.post("/jobs", json={"spec": _SPEC})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


# --- The happy path ----------------------------------------------------------


async def test_returns_the_latest_checkpoint_and_a_link(
    api_client: AsyncClient,
) -> None:
    job_id = await submit_job(api_client)
    entry = sample_entry(job_id)
    put_manifest(job_id, entry)
    StubCheckpointStore.sizes[entry["key"]] = 4_812_345

    response = await api_client.get(f"/jobs/{job_id}/checkpoint")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["key"] == entry["key"]
    assert body["step"] == 500
    assert body["epoch"] == 2
    assert body["loss"] == pytest.approx(0.1234)
    assert body["size_bytes"] == 4_812_345
    assert body["download_path"] == f"/jobs/{job_id}/checkpoint/download"
    assert body["filename"].startswith(f"model-{job_id[:8]}-e002-s00000500")
    assert body["filename"].endswith(".pt")
    assert "torch.save" in body["format"]


async def test_download_streams_the_blob_with_a_filename(
    api_client: AsyncClient,
) -> None:
    """The bytes come back whole, and named something identifiable."""
    job_id = await submit_job(api_client)
    entry = sample_entry(job_id)
    put_manifest(job_id, entry)
    payload = b"pretend-torch-checkpoint-bytes" * 10
    StubCheckpointStore.objects[entry["key"]] = payload
    StubCheckpointStore.sizes[entry["key"]] = len(payload)

    response = await api_client.get(f"/jobs/{job_id}/checkpoint/download")
    assert response.status_code == 200
    assert response.content == payload, "chunks must reassemble exactly"
    assert response.headers["content-type"] == "application/octet-stream"
    assert "attachment" in response.headers["content-disposition"]
    assert f"model-{job_id[:8]}" in response.headers["content-disposition"]
    assert StubCheckpointStore.streamed == [entry["key"]]


async def test_download_resolves_the_key_server_side(
    api_client: AsyncClient,
) -> None:
    """The caller chooses a job, never an object key.

    Otherwise this route would be a way to read any object in the checkpoints
    bucket by passing a crafted key.
    """
    job_id = await submit_job(api_client)
    entry = sample_entry(job_id)
    put_manifest(job_id, entry)
    StubCheckpointStore.objects[entry["key"]] = b"mine"
    StubCheckpointStore.objects["checkpoints/someone-else/secret.pt"] = b"theirs"

    response = await api_client.get(
        f"/jobs/{job_id}/checkpoint/download",
        params={"key": "checkpoints/someone-else/secret.pt"},
    )
    assert response.status_code == 200
    assert response.content == b"mine", "a query param must not steer the read"


async def test_download_without_a_checkpoint_is_404(api_client: AsyncClient) -> None:
    job_id = await submit_job(api_client)
    response = await api_client.get(f"/jobs/{job_id}/checkpoint/download")
    assert response.status_code == 404


async def test_unknown_size_is_reported_as_null_not_zero(
    api_client: AsyncClient,
) -> None:
    """A size that storage would not report is unknown, never a plausible number."""
    job_id = await submit_job(api_client)
    put_manifest(job_id, sample_entry(job_id))
    # No entry in `sizes` -> head returns None.

    response = await api_client.get(f"/jobs/{job_id}/checkpoint")
    assert response.status_code == 200
    assert response.json()["size_bytes"] is None


async def test_loss_may_be_absent_without_being_invented(
    api_client: AsyncClient,
) -> None:
    job_id = await submit_job(api_client)
    entry = sample_entry(job_id)
    del entry["loss"]
    put_manifest(job_id, entry)

    response = await api_client.get(f"/jobs/{job_id}/checkpoint")
    assert response.status_code == 200
    assert response.json()["loss"] is None


# --- Nothing to download -----------------------------------------------------


async def test_no_manifest_is_a_404(api_client: AsyncClient) -> None:
    """Ordinary: checkpointing needs S3 configured on the peer that ran the job."""
    job_id = await submit_job(api_client)
    response = await api_client.get(f"/jobs/{job_id}/checkpoint")
    assert response.status_code == 404
    assert "no saved model" in response.json()["detail"]


async def test_manifest_without_a_latest_entry_is_a_404(
    api_client: AsyncClient,
) -> None:
    job_id = await submit_job(api_client)
    put_manifest(job_id, None)
    response = await api_client.get(f"/jobs/{job_id}/checkpoint")
    assert response.status_code == 404


async def test_unknown_job_is_a_404_naming_the_job(api_client: AsyncClient) -> None:
    response = await api_client.get(f"/jobs/{uuid.uuid4()}/checkpoint")
    assert response.status_code == 404
    assert response.json()["detail"] == "unknown job"


# --- Storage being down is not "no model" ------------------------------------


async def test_unreachable_storage_is_503_not_404(api_client: AsyncClient) -> None:
    """The distinction that saves someone an hour.

    Reporting "no saved model" when the truth is that MinIO is down would send
    them hunting for a training bug that does not exist.
    """
    job_id = await submit_job(api_client)
    put_manifest(job_id, sample_entry(job_id))
    StubCheckpointStore.unreachable = True

    response = await api_client.get(f"/jobs/{job_id}/checkpoint")
    assert response.status_code == 503
    assert "unreachable" in response.json()["detail"]


# --- The gate ----------------------------------------------------------------


async def test_anonymous_is_refused(anon_client: AsyncClient) -> None:
    """The jobs router requires a user; this route inherits that."""
    response = await anon_client.get(f"/jobs/{uuid.uuid4()}/checkpoint")
    assert response.status_code == 401


async def test_a_node_token_cannot_read_it(anon_client: AsyncClient) -> None:
    """Audience separation: this is a human endpoint, not an agent one."""
    from tests.helpers import auth_headers, register_new_node

    reg, _key = await register_new_node(anon_client)
    response = await anon_client.get(
        f"/jobs/{uuid.uuid4()}/checkpoint",
        headers=auth_headers(reg["access_token"]),
    )
    assert response.status_code in (401, 403)
