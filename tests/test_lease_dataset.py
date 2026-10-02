"""A peer fetches its job's uploaded dataset through the orchestrator.

The presigned MinIO URL names the operator's LAN address, which a peer joining
over a tunnel cannot reach and which is plain HTTP on the LAN. Real Postgres,
real claims; only MinIO is stubbed, for the reason test_checkpoint_objects.py
gives.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api import leases as leases_api
from orchestrator.core.config import get_settings
from orchestrator.models.dataset import Dataset
from orchestrator.models.lease import Lease
from orchestrator.services.leases import sweep_expired_leases
from orchestrator.services.object_store import ObjectNotFoundError
from orchestrator.services.scheduling import run_scheduler_pass
from tests.helpers import (
    auth_headers,
    register_new_node,
    schedule_single_rank_job,
    send_heartbeat,
)

ARCHIVE = b"PK\x03\x04" + bytes(range(256)) * 40  # several chunks' worth


class StubDatasetBucket:
    objects: dict[str, bytes] = {}

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def head_size_bytes(self, *, key: str) -> int | None:
        blob = type(self).objects.get(key)
        return len(blob) if blob is not None else None

    def iter_object(self, *, key: str, chunk_bytes: int = 1024 * 1024):  # type: ignore[no-untyped-def]
        blob = type(self).objects.get(key)
        if blob is None:
            raise ObjectNotFoundError(key)
        for i in range(0, len(blob), 1000):
            yield blob[i : i + 1000]

    def presigned_get_url(self, *, key: str, expires_seconds: int) -> str:
        return f"http://minio.invalid/{key}"


@pytest.fixture(autouse=True)
def stub_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    StubDatasetBucket.objects = {}
    monkeypatch.setattr(leases_api, "DatasetObjectStore", StubDatasetBucket)


async def _claim_custom_job(
    api_client: AsyncClient, session: AsyncSession
) -> tuple[dict[str, Any], dict[str, Any], Dataset]:
    dataset = Dataset(
        id=uuid.uuid4(),
        name=f"upload-{uuid.uuid4().hex[:6]}",
        object_key=f"datasets/{uuid.uuid4().hex}.zip",
        sha256="0" * 64,
        size_bytes=len(ARCHIVE),
        classes=["cat", "dog"],
        train_images=4,
        test_images=2,
        per_class_counts={},
        created_by="test",
    )
    session.add(dataset)
    StubDatasetBucket.objects[dataset.object_key] = ARCHIVE
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id, token = reg["node_id"], reg["access_token"]
    await send_heartbeat(api_client, node_id=node_id, token=token, gpu_util=5.0)
    spec = {
        "dataset_id": str(dataset.id),
        "model": "small_cnn",
        "epochs": 1,
        "batch_size": 8,
        "learning_rate": 0.01,
        "world_size": 1,
        "min_gpu_mem_bytes": None,
    }
    schedule_single_rank_job(session, node_id=uuid.UUID(node_id), spec=spec)
    await session.commit()
    resp = await api_client.post(f"/nodes/{node_id}/leases/claim", headers=auth_headers(token))
    return reg, resp.json(), dataset


async def test_the_live_attempt_streams_its_dataset(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    _reg, claim, _dataset = await _claim_custom_job(api_client, session)
    resp = await api_client.get(
        f"/leases/{claim['lease']['id']}/dataset",
        headers=auth_headers(claim["checkpoint_token"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == ARCHIVE
    assert resp.headers["content-length"] == str(len(ARCHIVE))


async def test_no_token_or_a_node_token_is_401(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    reg, claim, _dataset = await _claim_custom_job(api_client, session)
    url = f"/leases/{claim['lease']['id']}/dataset"
    assert (await api_client.get(url)).status_code == 401
    node_token = auth_headers(reg["access_token"])
    assert (await api_client.get(url, headers=node_token)).status_code == 401


async def test_a_superseded_attempt_cannot_download(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    settings = get_settings()
    reg, claim1, _dataset = await _claim_custom_job(api_client, session)
    await session.execute(
        update(Lease)
        .where(Lease.id == uuid.UUID(claim1["lease"]["id"]))
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()
    await sweep_expired_leases(session, settings=settings)
    await session.commit()
    await run_scheduler_pass(session, settings=settings)
    await session.commit()
    resp = await api_client.get(
        f"/leases/{claim1['lease']['id']}/dataset",
        headers=auth_headers(claim1["checkpoint_token"]),
    )
    assert resp.status_code == 409


async def test_a_deleted_dataset_is_404(api_client: AsyncClient, session: AsyncSession) -> None:
    _reg, claim, dataset = await _claim_custom_job(api_client, session)
    del StubDatasetBucket.objects[dataset.object_key]
    resp = await api_client.get(
        f"/leases/{claim['lease']['id']}/dataset",
        headers=auth_headers(claim["checkpoint_token"]),
    )
    assert resp.status_code == 404
