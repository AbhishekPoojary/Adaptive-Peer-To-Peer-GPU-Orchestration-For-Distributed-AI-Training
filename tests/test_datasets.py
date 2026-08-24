"""Dataset upload, job integration, and claim-time delivery (ADR-014).

Archive validation itself is covered against real malicious zips in
``test_dataset_archive.py``; this file exercises the surface around it — who may
upload, what a job may reference, and what a peer is actually handed when it
claims work.

``StubObjectStore`` stands in for MinIO. That is a deliberate exception to this
project's preference for real dependencies: the suite provisions a real Postgres
because the concurrency guarantees under test *are* Postgres semantics, whereas
nothing asserted here depends on S3 behaviour — only on whether the orchestrator
calls it, and with what. Making the whole suite require a running MinIO to test
a presigned-URL string would buy no extra confidence.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from typing import Any

import pytest
from httpx import AsyncClient

from orchestrator.api import datasets as datasets_api
from orchestrator.api import leases as leases_api
from tests.helpers import auth_headers, register_new_node, send_heartbeat

_SPEC: dict[str, Any] = {
    "dataset": "cifar10",
    "model": "small_cnn",
    "epochs": 1,
    "batch_size": 8,
    "learning_rate": 0.01,
    "world_size": 1,
    "min_gpu_mem_bytes": None,
}


class StubObjectStore:
    """Records what would have been stored, without an S3 anywhere.

    Class-level state so a test can inspect it after the request has finished
    and the instance the endpoint constructed has been discarded.
    """

    uploaded: list[tuple[str, str]] = []
    deleted: list[str] = []
    signed: list[str] = []
    fail_upload = False

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def ensure_bucket(self) -> None:
        return None

    def upload_file(self, *, local_path: str, key: str) -> None:
        if type(self).fail_upload:
            from orchestrator.services.object_store import ObjectStoreError

            raise ObjectStoreError("stubbed storage outage")
        type(self).uploaded.append((local_path, key))

    def delete_object(self, *, key: str) -> None:
        type(self).deleted.append(key)

    def presigned_get_url(self, *, key: str, expires_seconds: int) -> str:
        type(self).signed.append(key)
        return f"https://stub-storage.invalid/{key}?expires={expires_seconds}"


@pytest.fixture(autouse=True)
def stub_object_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point both call sites at the stub and reset its recorded state."""
    StubObjectStore.uploaded = []
    StubObjectStore.deleted = []
    StubObjectStore.signed = []
    StubObjectStore.fail_upload = False
    monkeypatch.setattr(datasets_api, "DatasetObjectStore", StubObjectStore)
    monkeypatch.setattr(leases_api, "DatasetObjectStore", StubObjectStore)


def make_archive(
    names: list[str] | None = None, *, wrap: str | None = None
) -> bytes:
    """Build a valid ImageFolder zip in memory."""
    entries = names or [
        "train/cat/a.png",
        "train/cat/b.png",
        "train/dog/c.png",
        "test/cat/d.png",
        "test/dog/e.png",
    ]
    if wrap:
        entries = [f"{wrap}/{name}" for name in entries]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for entry in entries:
            zf.writestr(entry, b"not-really-an-image-but-never-decompressed")
    return buffer.getvalue()


async def upload(
    client: AsyncClient,
    *,
    name: str = "pets",
    token: str | None = None,
    content: bytes | None = None,
    description: str | None = None,
) -> Any:
    data: dict[str, str] = {"name": name}
    if description is not None:
        data["description"] = description
    return await client.post(
        "/datasets",
        data=data,
        files={"file": (f"{name}.zip", content or make_archive(), "application/zip")},
        headers=auth_headers(token) if token else {},
    )



async def online_node(client: AsyncClient) -> tuple[str, str]:
    """Register and heartbeat one GPU node so it is ONLINE. Returns (id, token)."""
    reg, _key = await register_new_node(client, with_gpu=True)
    await send_heartbeat(
        client, node_id=reg["node_id"], token=reg["access_token"], gpu_util=10.0
    )
    return reg["node_id"], reg["access_token"]


# --- Upload ------------------------------------------------------------------


async def test_admin_can_upload_a_dataset(anon_client: AsyncClient) -> None:
    response = await upload(anon_client, token=anon_client.admin_token)  # type: ignore[attr-defined]
    assert response.status_code == 201, response.text

    body = response.json()
    dataset = body["dataset"]
    assert dataset["name"] == "pets"
    assert dataset["classes"] == ["cat", "dog"]
    assert dataset["num_classes"] == 2
    assert dataset["train_images"] == 3
    assert dataset["test_images"] == 2
    assert dataset["created_by"] == "pytest-admin"
    assert body["summary"] == "2 classes, 3 training and 2 test images"

    # The archive reached storage under a server-chosen key.
    assert len(StubObjectStore.uploaded) == 1
    _path, key = StubObjectStore.uploaded[0]
    assert key.startswith("datasets/") and key.endswith("/archive.zip")
    assert dataset["id"] in key


async def test_operator_cannot_upload(api_client: AsyncClient) -> None:
    """Uploading is ADMIN-only: a dataset runs on other people's machines."""
    response = await upload(api_client)
    assert response.status_code == 403
    assert StubObjectStore.uploaded == []


async def test_anonymous_cannot_upload(anon_client: AsyncClient) -> None:
    response = await upload(anon_client)
    assert response.status_code == 401


async def test_invalid_archive_is_refused_with_a_usable_message(
    anon_client: AsyncClient,
) -> None:
    """The uploader is the only one who can fix it, so the error must be specific."""
    bad = make_archive(["train/cat/a.png", "train/dog/b.png"])  # no test/ split
    response = await upload(
        anon_client, token=anon_client.admin_token, content=bad  # type: ignore[attr-defined]
    )
    assert response.status_code == 422
    assert "no test/ split" in response.json()["detail"]
    # Nothing invalid reaches the bucket.
    assert StubObjectStore.uploaded == []


async def test_duplicate_name_conflicts(anon_client: AsyncClient) -> None:
    token = anon_client.admin_token  # type: ignore[attr-defined]
    assert (await upload(anon_client, token=token)).status_code == 201
    second = await upload(anon_client, token=token)
    assert second.status_code == 409
    assert "already exists" in second.json()["detail"]


async def test_storage_failure_leaves_no_row(anon_client: AsyncClient) -> None:
    """A valid archive that cannot be stored must not leave a phantom dataset.

    Otherwise the picker would offer a dataset whose bytes were never written,
    and every job using it would fail on a peer.
    """
    StubObjectStore.fail_upload = True
    token = anon_client.admin_token  # type: ignore[attr-defined]
    response = await upload(anon_client, token=token)
    assert response.status_code == 503

    listing = await anon_client.get("/datasets", headers=auth_headers(token))
    assert listing.json()["datasets"] == []


async def test_wrapped_directory_is_accepted(anon_client: AsyncClient) -> None:
    """Zipping the folder rather than its contents is the commonest near-miss."""
    response = await upload(
        anon_client,
        token=anon_client.admin_token,  # type: ignore[attr-defined]
        content=make_archive(wrap="my-pets"),
    )
    assert response.status_code == 201
    assert response.json()["dataset"]["classes"] == ["cat", "dog"]


@pytest.mark.parametrize("bad_name", ["a", "has space", "has/slash", "../escape"])
async def test_rejects_unusable_names(anon_client: AsyncClient, bad_name: str) -> None:
    response = await upload(
        anon_client, name=bad_name, token=anon_client.admin_token  # type: ignore[attr-defined]
    )
    assert response.status_code == 422


# --- Listing, detail, delete -------------------------------------------------


async def test_operator_can_list_and_read(api_client: AsyncClient) -> None:
    """An OPERATOR must see datasets — otherwise they cannot submit a job."""
    admin = api_client.admin_token  # type: ignore[attr-defined]
    created = await upload(api_client, token=admin)
    dataset_id = created.json()["dataset"]["id"]

    listing = await api_client.get("/datasets")
    assert listing.status_code == 200
    assert [d["name"] for d in listing.json()["datasets"]] == ["pets"]

    detail = await api_client.get(f"/datasets/{dataset_id}")
    assert detail.status_code == 200
    assert detail.json()["per_class_counts"]["train"] == {"cat": 2, "dog": 1}


async def test_response_never_exposes_the_object_key(api_client: AsyncClient) -> None:
    """Where the archive sits in the bucket is the server's business."""
    admin = api_client.admin_token  # type: ignore[attr-defined]
    created = await upload(api_client, token=admin)
    assert "object_key" not in created.json()["dataset"]

    detail = await api_client.get(f"/datasets/{created.json()['dataset']['id']}")
    assert "object_key" not in detail.json()


async def test_delete_hides_it_and_removes_the_object(api_client: AsyncClient) -> None:
    admin = api_client.admin_token  # type: ignore[attr-defined]
    created = await upload(api_client, token=admin)
    dataset_id = created.json()["dataset"]["id"]

    deleted = await api_client.delete(
        f"/datasets/{dataset_id}", headers=auth_headers(admin)
    )
    assert deleted.status_code == 204
    assert len(StubObjectStore.deleted) == 1

    assert (await api_client.get("/datasets")).json()["datasets"] == []
    assert (await api_client.get(f"/datasets/{dataset_id}")).status_code == 404


async def test_operator_cannot_delete(api_client: AsyncClient) -> None:
    admin = api_client.admin_token  # type: ignore[attr-defined]
    created = await upload(api_client, token=admin)
    response = await api_client.delete(f"/datasets/{created.json()['dataset']['id']}")
    assert response.status_code == 403


# --- Job submission ----------------------------------------------------------


async def test_builtin_dataset_submission_is_unchanged(
    api_client: AsyncClient,
) -> None:
    """The regression that matters most: 89 historical jobs use this shape.

    JobSpec forbids extra fields, so a careless change here would 422 every
    existing caller — the bench harness included.
    """
    response = await api_client.post("/jobs", json={"spec": _SPEC})
    assert response.status_code == 201, response.text
    assert response.json()["spec"]["dataset"] == "cifar10"


async def test_can_submit_against_an_uploaded_dataset(api_client: AsyncClient) -> None:
    admin = api_client.admin_token  # type: ignore[attr-defined]
    dataset_id = (await upload(api_client, token=admin)).json()["dataset"]["id"]

    spec = {**_SPEC, "dataset": None, "dataset_id": dataset_id}
    response = await api_client.post("/jobs", json={"spec": spec})
    assert response.status_code == 201, response.text
    assert response.json()["spec"]["dataset_id"] == dataset_id


async def test_unknown_dataset_is_refused_at_submit(api_client: AsyncClient) -> None:
    """Caught now, not twenty minutes later on a peer nobody is watching."""
    spec = {**_SPEC, "dataset": None, "dataset_id": str(uuid.uuid4())}
    response = await api_client.post("/jobs", json={"spec": spec})
    assert response.status_code == 422
    assert "unknown or deleted dataset" in response.json()["detail"]


async def test_deleted_dataset_cannot_start_new_jobs(api_client: AsyncClient) -> None:
    admin = api_client.admin_token  # type: ignore[attr-defined]
    dataset_id = (await upload(api_client, token=admin)).json()["dataset"]["id"]
    await api_client.delete(f"/datasets/{dataset_id}", headers=auth_headers(admin))

    spec = {**_SPEC, "dataset": None, "dataset_id": dataset_id}
    response = await api_client.post("/jobs", json={"spec": spec})
    assert response.status_code == 422


async def test_requires_exactly_one_dataset_source(api_client: AsyncClient) -> None:
    """Neither is unrunnable; both is ambiguous and would resolve silently."""
    neither = {**_SPEC, "dataset": None}
    response = await api_client.post("/jobs", json={"spec": neither})
    assert response.status_code == 422
    assert "a job needs a dataset" in response.text

    admin = api_client.admin_token  # type: ignore[attr-defined]
    dataset_id = (await upload(api_client, token=admin)).json()["dataset"]["id"]
    both = {**_SPEC, "dataset": "mnist", "dataset_id": dataset_id}
    response = await api_client.post("/jobs", json={"spec": both})
    assert response.status_code == 422
    assert "not both" in response.text


# --- What the peer is handed at claim time -----------------------------------


async def test_claim_hands_the_peer_a_signed_url_and_digest(
    api_client: AsyncClient,
) -> None:
    """The peer needs both: the URL to fetch, the digest to trust what it got."""
    admin = api_client.admin_token  # type: ignore[attr-defined]
    dataset = (await upload(api_client, token=admin)).json()["dataset"]

    node_id, node_token = await online_node(api_client)

    spec = {**_SPEC, "dataset": None, "dataset_id": dataset["id"]}
    submitted = await api_client.post(
        "/jobs", json={"spec": spec, "scheduler_name": "least_loaded"}
    )
    assert submitted.status_code == 201, submitted.text

    claim = await api_client.post(
        f"/nodes/{node_id}/leases/claim", headers=auth_headers(node_token)
    )
    assert claim.status_code == 200, claim.text
    granted = claim.json()
    assert granted["lease"] is not None, "expected the job to be granted"

    job_spec = granted["job_spec"]
    assert job_spec["dataset_url"].startswith("https://stub-storage.invalid/")
    assert job_spec["dataset_sha256"] == dataset["sha256"]
    assert job_spec["dataset_num_classes"] == 2
    assert job_spec["dataset_name"] == "pets"


async def test_claim_for_a_builtin_dataset_injects_nothing(
    api_client: AsyncClient,
) -> None:
    """A built-in is downloaded by the trainer itself; no URL should appear."""
    node_id, node_token = await online_node(api_client)

    await api_client.post(
        "/jobs", json={"spec": _SPEC, "scheduler_name": "least_loaded"}
    )
    claim = await api_client.post(
        f"/nodes/{node_id}/leases/claim", headers=auth_headers(node_token)
    )
    assert claim.status_code == 200
    job_spec = claim.json()["job_spec"]
    assert "dataset_url" not in job_spec
    assert StubObjectStore.signed == []


async def test_signed_url_is_minted_per_claim(api_client: AsyncClient) -> None:
    """Not baked into the stored spec.

    A spec is kept forever; a signed URL in it would be a long-lived credential
    sitting in the jobs table. Minting per claim is what keeps it expiring.
    """
    admin = api_client.admin_token  # type: ignore[attr-defined]
    dataset_id = (await upload(api_client, token=admin)).json()["dataset"]["id"]

    node_id, node_token = await online_node(api_client)

    spec = {**_SPEC, "dataset": None, "dataset_id": dataset_id}
    submitted = await api_client.post(
        "/jobs", json={"spec": spec, "scheduler_name": "least_loaded"}
    )
    stored_spec = submitted.json()["spec"]
    assert "dataset_url" not in stored_spec, "a signed URL must not be persisted"

    await api_client.post(
        f"/nodes/{node_id}/leases/claim", headers=auth_headers(node_token)
    )
    assert StubObjectStore.signed, "claim should have signed a URL"
