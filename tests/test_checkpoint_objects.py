"""Trainers checkpoint through the orchestrator (ADR-006 addendum 3).

Each test drives the real endpoints against a real Postgres, so the access
decision is made on genuine lease rows: granted by a real claim, fenced by the
real sweep and scheduler. Only MinIO is replaced, by ``StubCheckpointBucket``,
for the reason ``test_checkpoint_download.py`` gives: what is under test is
which requests are allowed, not how S3 stores bytes.
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
from orchestrator.models.lease import Lease
from orchestrator.services.checkpoint_access import key_belongs_to_job
from orchestrator.services.leases import sweep_expired_leases
from orchestrator.services.object_store import ObjectNotFoundError
from orchestrator.services.scheduling import run_scheduler_pass
from tests.helpers import (
    auth_headers,
    register_new_node,
    schedule_single_rank_job,
    send_heartbeat,
)


class StubCheckpointBucket:
    """In-memory stand-in for the checkpoints bucket."""

    objects: dict[str, bytes] = {}

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def ensure_bucket(self) -> None:
        pass

    def put_bytes(self, *, key: str, data: bytes) -> None:
        type(self).objects[key] = data

    def get_bytes(self, *, key: str) -> bytes:
        try:
            return type(self).objects[key]
        except KeyError as exc:
            raise ObjectNotFoundError(key) from exc


@pytest.fixture(autouse=True)
def stub_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    StubCheckpointBucket.objects = {}
    monkeypatch.setattr(leases_api, "CheckpointObjectStore", StubCheckpointBucket)


async def _granted(
    api_client: AsyncClient, session: AsyncSession
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A node with a real claimed lease. Returns (registration, claim body)."""
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id, token = reg["node_id"], reg["access_token"]
    await send_heartbeat(api_client, node_id=node_id, token=token, gpu_util=5.0)
    schedule_single_rank_job(session, node_id=uuid.UUID(node_id), submitted_by="ckpt-test")
    await session.commit()
    resp = await api_client.post(
        f"/nodes/{node_id}/leases/claim", headers=auth_headers(token)
    )
    resp.raise_for_status()
    body = resp.json()
    assert body["lease"] is not None
    return reg, body


def _url(claim: dict[str, Any], key: str) -> str:
    return f"/leases/{claim['lease']['id']}/checkpoint-objects/{key}"


def _bearer(claim: dict[str, Any]) -> dict[str, str]:
    return auth_headers(claim["checkpoint_token"])


# --- The path a real trainer takes --------------------------------------------


async def test_claim_carries_a_checkpoint_token_outside_the_spec(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    _reg, claim = await _granted(api_client, session)
    assert claim["checkpoint_token"]
    # Not in the spec: the spec is logged by the agent and stored on the job.
    assert "checkpoint_token" not in claim["job_spec"]


async def test_write_then_read_back_through_the_api(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    _reg, claim = await _granted(api_client, session)
    job_id = claim["lease"]["job_id"]
    blob_key = f"checkpoints/{job_id}/e001-s00000100-abcd1234.pt"

    put = await api_client.put(_url(claim, blob_key), content=b"weights", headers=_bearer(claim))
    assert put.status_code == 204, put.text
    assert StubCheckpointBucket.objects[blob_key] == b"weights"

    got = await api_client.get(_url(claim, blob_key), headers=_bearer(claim))
    assert got.status_code == 200
    assert got.content == b"weights"


async def test_missing_manifest_is_404_meaning_first_attempt(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    _reg, claim = await _granted(api_client, session)
    key = f"manifests/{claim['lease']['job_id']}.json"
    resp = await api_client.get(_url(claim, key), headers=_bearer(claim))
    assert resp.status_code == 404


async def test_the_trainers_own_writer_round_trips(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """``trainer.checkpoint.save_checkpoint`` and ``latest_entry`` over the API,
    so a change to the manifest's keys cannot pass these tests while breaking
    the access rules (or the reverse)."""
    from trainer import checkpoint as ckpt

    _reg, claim = await _granted(api_client, session)
    job_id = claim["lease"]["job_id"]
    written: list[tuple[str, bytes]] = []

    class Recorder:
        """The writer is synchronous and this client is not, so record the
        writes in order and replay them over HTTP."""

        def put_bytes(self, key: str, data: bytes) -> None:
            written.append((key, data))

        def get_bytes(self, key: str) -> bytes:
            raise ckpt.ObjectNotFoundError(key)  # first attempt: no manifest yet

    entry = ckpt.save_checkpoint(
        Recorder(),
        job_id=job_id,
        blob=b"model",
        step=100,
        epoch=1,
        world_size=1,
        lease_epoch=1,
        loss=0.5,
    )
    for key, data in written:
        resp = await api_client.put(_url(claim, key), content=data, headers=_bearer(claim))
        assert resp.status_code == 204, (key, resp.text)

    manifest = await api_client.get(
        _url(claim, f"manifests/{job_id}.json"), headers=_bearer(claim)
    )
    assert manifest.status_code == 200
    assert manifest.json()["latest"]["key"] == entry.key


# --- Who is refused -----------------------------------------------------------


async def test_no_token_is_401(api_client: AsyncClient, session: AsyncSession) -> None:
    _reg, claim = await _granted(api_client, session)
    key = f"manifests/{claim['lease']['job_id']}.json"
    resp = await api_client.get(_url(claim, key))
    assert resp.status_code == 401


async def test_a_node_token_is_not_a_checkpoint_token(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """Same signing key, different audience: a node's own access token must not
    open the checkpoint endpoints."""
    reg, claim = await _granted(api_client, session)
    key = f"manifests/{claim['lease']['job_id']}.json"
    resp = await api_client.get(_url(claim, key), headers=auth_headers(reg["access_token"]))
    assert resp.status_code == 401


async def test_token_for_another_lease_is_403(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    _reg_a, claim_a = await _granted(api_client, session)
    _reg_b, claim_b = await _granted(api_client, session)
    key = f"manifests/{claim_b['lease']['job_id']}.json"
    resp = await api_client.get(_url(claim_b, key), headers=_bearer(claim_a))
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "make_key",
    [
        lambda job, other: f"manifests/{other}.json",
        lambda job, other: f"checkpoints/{other}/e001-s1-x.pt",
        lambda job, other: f"checkpoints/{job}/../{other}/e001-s1-x.pt",
        lambda job, other: f"checkpoints/{job}/nested/e001.pt",
        lambda job, other: "datasets/anything.zip",
    ],
    ids=["other-manifest", "other-blob", "traversal", "nested", "outside"],
)
async def test_keys_outside_the_job_are_403(
    api_client: AsyncClient, session: AsyncSession, make_key: Any
) -> None:
    _reg, claim = await _granted(api_client, session)
    key = make_key(claim["lease"]["job_id"], str(uuid.uuid4()))
    resp = await api_client.put(_url(claim, key), content=b"x", headers=_bearer(claim))
    # A traversal may be normalised away by the HTTP layer before routing and
    # never reach the handler; either way it must not be stored.
    assert resp.status_code in (403, 404), (key, resp.status_code)
    assert StubCheckpointBucket.objects == {}


async def test_a_superseded_attempt_can_neither_read_nor_write(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    """The point of checking the lease per request rather than trusting the
    token: once the detector has replaced an attempt, its trainer is shut out
    although its token is still within its expiry."""
    settings = get_settings()
    reg, claim1 = await _granted(api_client, session)
    node_id, token = reg["node_id"], reg["access_token"]
    job_id = claim1["lease"]["job_id"]

    await session.execute(
        update(Lease)
        .where(Lease.id == uuid.UUID(claim1["lease"]["id"]))
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await session.commit()
    assert await sweep_expired_leases(session, settings=settings) == 1
    await session.commit()
    assert await run_scheduler_pass(session, settings=settings) == 1
    await session.commit()
    resp = await api_client.post(f"/nodes/{node_id}/leases/claim", headers=auth_headers(token))
    claim2 = resp.json()
    assert claim2["lease"]["lease_epoch"] == 2

    blob = f"checkpoints/{job_id}/e001-s00000100-zombie00.pt"
    zombie_write = await api_client.put(
        _url(claim1, blob), content=b"stale", headers=_bearer(claim1)
    )
    assert zombie_write.status_code == 409
    zombie_read = await api_client.get(
        _url(claim1, f"manifests/{job_id}.json"), headers=_bearer(claim1)
    )
    assert zombie_read.status_code == 409
    assert StubCheckpointBucket.objects == {}

    # The live attempt is unaffected.
    live = await api_client.put(_url(claim2, blob), content=b"fresh", headers=_bearer(claim2))
    assert live.status_code == 204


async def test_only_rank_zero_writes(api_client: AsyncClient, session: AsyncSession) -> None:
    _reg, claim = await _granted(api_client, session)
    await session.execute(
        update(Lease).where(Lease.id == uuid.UUID(claim["lease"]["id"])).values(rank=1)
    )
    await session.commit()
    job_id = claim["lease"]["job_id"]
    write = await api_client.put(
        _url(claim, f"checkpoints/{job_id}/e001-s1-a.pt"), content=b"x", headers=_bearer(claim)
    )
    assert write.status_code == 403
    # Reading is every rank's business: all of them restore on resume.
    read = await api_client.get(_url(claim, f"manifests/{job_id}.json"), headers=_bearer(claim))
    assert read.status_code == 404


async def test_oversized_object_is_413(
    api_client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "checkpoint_max_bytes", 8)
    _reg, claim = await _granted(api_client, session)
    key = f"checkpoints/{claim['lease']['job_id']}/e001-s1-a.pt"
    resp = await api_client.put(_url(claim, key), content=b"123456789", headers=_bearer(claim))
    assert resp.status_code == 413
    assert StubCheckpointBucket.objects == {}


# --- The key rule on its own ----------------------------------------------------


def test_key_rule() -> None:
    job = str(uuid.uuid4())
    assert key_belongs_to_job(f"manifests/{job}.json", job)
    assert key_belongs_to_job(f"checkpoints/{job}/e001-s00000100-abcd1234.pt", job)
    for bad in (
        "",
        f"checkpoints/{job}/",
        f"/manifests/{job}.json",
        f"manifests/{job}.json/../x",
        f"checkpoints/{job}/../other/x.pt",
        f"checkpoints/{job}/./x.pt",
        f"checkpoints/{job}\\x.pt",
        f"checkpoints/{job}/a/b.pt",
        f"checkpoints/{job}x/a.pt",
        f"manifests/{job}.json.bak",
    ):
        assert not key_belongs_to_job(bad, job), bad
