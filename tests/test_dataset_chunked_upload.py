"""Chunked, resumable dataset upload (ADR-014, amended).

The reason this exists is a transport limit, not a preference: the dashboard is
shared through a Cloudflare quick tunnel so a collaborator on another network
can reach it, and that tunnel cuts a transfer off after a minute or two. A
single request carrying a large archive cannot succeed over it however many
times it is retried.

So the cases here are mostly about what goes wrong mid-transfer -- a chunk sent
twice, a chunk missing, a client that stops and comes back -- plus the ceilings,
which matter more on this path than on the single-shot one. A session is disk
handed out *before* anything about the archive has been proved, which makes it
the one upload surface that can be made expensive without ever being valid.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest
from httpx import AsyncClient

from orchestrator.api import datasets as datasets_api
from orchestrator.api import leases as leases_api
from orchestrator.core.db import get_sessionmaker
from orchestrator.models.user import UserRole
from tests.helpers import auth_headers, seed_user, user_token
from tests.test_datasets import StubObjectStore
from tests.test_datasets import upload as single_shot_upload


@pytest.fixture(autouse=True)
def stub_object_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same stand-in for MinIO the single-shot tests use.

    Declared again here rather than shared: an autouse fixture only applies to
    the module that defines it, and without it these tests would reach for a
    real bucket.
    """
    StubObjectStore.uploaded = []
    StubObjectStore.deleted = []
    StubObjectStore.signed = []
    StubObjectStore.fail_upload = False
    monkeypatch.setattr(datasets_api, "DatasetObjectStore", StubObjectStore)
    monkeypatch.setattr(leases_api, "DatasetObjectStore", StubObjectStore)


async def another_admin_token() -> str:
    """A second ADMIN, for proving one admin cannot reach another's upload."""
    maker = get_sessionmaker()
    async with maker() as setup_session:
        other = await seed_user(
            setup_session, username="second-admin", role=UserRole.ADMIN
        )
    return user_token(other)

_SPEC_CHUNK = 4 * 1024 * 1024


def make_archive(names: list[str] | None = None, *, pad: int = 0) -> bytes:
    """A valid ImageFolder zip, optionally padded to force several chunks."""
    entries = names or [
        "train/cat/a.png",
        "train/cat/b.png",
        "train/dog/c.png",
        "test/cat/d.png",
        "test/dog/e.png",
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for entry in entries:
            zf.writestr(entry, b"not-really-an-image-but-never-decompressed")
        if pad:
            # Stored, not deflated: the point is to make the file genuinely
            # large enough to span chunks, and a compressible pad would not.
            zf.writestr(
                zipfile.ZipInfo("train/cat/pad.png"),
                b"\xff" * pad,
                compress_type=zipfile.ZIP_STORED,
            )
    return buffer.getvalue()


async def open_session(
    client: AsyncClient, *, token: str, name: str = "chunked", total: int
) -> Any:
    return await client.post(
        "/datasets/uploads",
        json={"name": name, "total_bytes": total},
        headers=auth_headers(token),
    )


async def send_chunk(
    client: AsyncClient, *, token: str, upload_id: str, index: int, payload: bytes
) -> Any:
    return await client.put(
        f"/datasets/uploads/{upload_id}/chunks/{index}",
        content=payload,
        headers=auth_headers(token),
    )


async def upload_in_chunks(
    client: AsyncClient, *, token: str, content: bytes, name: str = "chunked"
) -> Any:
    """The whole client-side dance: open, send every chunk, complete."""
    opened = await open_session(client, token=token, name=name, total=len(content))
    assert opened.status_code == 201, opened.text
    body = opened.json()
    size = body["chunk_bytes"]
    for index in range(body["total_chunks"]):
        piece = content[index * size : (index + 1) * size]
        response = await send_chunk(
            client, token=token, upload_id=body["upload_id"], index=index, payload=piece
        )
        assert response.status_code == 204, response.text
    return await client.post(
        f"/datasets/uploads/{body['upload_id']}/complete", headers=auth_headers(token)
    )


# --- The happy path ----------------------------------------------------------


async def test_an_archive_sent_in_chunks_becomes_a_dataset(
    anon_client: AsyncClient,
) -> None:
    token = anon_client.admin_token  # type: ignore[attr-defined]
    content = make_archive(pad=_SPEC_CHUNK + 1024)
    # Asserted locally rather than by opening a session for the answer, which
    # would leave a session nothing completes sitting on disk until it expires.
    assert len(content) > _SPEC_CHUNK, "fixture must span more than one chunk"

    response = await upload_in_chunks(anon_client, token=token, content=content)
    assert response.status_code == 201, response.text

    dataset = response.json()["dataset"]
    assert dataset["classes"] == ["cat", "dog"]
    # Reassembled byte for byte, which is what lets the peer's digest check
    # mean anything at the far end.
    assert dataset["size_bytes"] == len(content)
    # cat holds a, b and the pad image; dog holds c.
    assert dataset["train_images"] == 4
    assert dataset["test_images"] == 2


async def test_a_chunked_upload_reaches_object_storage_once(
    anon_client: AsyncClient,
) -> None:
    token = anon_client.admin_token  # type: ignore[attr-defined]
    response = await upload_in_chunks(
        anon_client, token=token, content=make_archive(), name="stored"
    )
    assert response.status_code == 201
    assert len(StubObjectStore.uploaded) == 1


# --- Interruption, which is the whole reason this path exists -----------------


async def test_resending_a_chunk_replaces_it_rather_than_failing(
    anon_client: AsyncClient,
) -> None:
    """A chunk whose request died partway is the case this must survive.

    The client cannot tell a chunk that arrived from one that was cut off just
    before the server wrote it, so its only sane move is to send it again. If
    that were an error, every flaky transfer would become an unrecoverable one.
    """
    token = anon_client.admin_token  # type: ignore[attr-defined]
    content = make_archive()
    opened = (await open_session(anon_client, token=token, total=len(content))).json()

    for _ in range(3):
        again = await send_chunk(
            anon_client,
            token=token,
            upload_id=opened["upload_id"],
            index=0,
            payload=content,
        )
        assert again.status_code == 204

    completed = await anon_client.post(
        f"/datasets/uploads/{opened['upload_id']}/complete",
        headers=auth_headers(token),
    )
    assert completed.status_code == 201, completed.text
    assert completed.json()["dataset"]["size_bytes"] == len(content)


async def test_status_reports_what_arrived_so_a_client_can_resume(
    anon_client: AsyncClient,
) -> None:
    token = anon_client.admin_token  # type: ignore[attr-defined]
    content = make_archive(pad=_SPEC_CHUNK * 2)
    opened = (await open_session(anon_client, token=token, total=len(content))).json()
    size = opened["chunk_bytes"]

    # Send every chunk but the second, as an interrupted client would have.
    for index in range(opened["total_chunks"]):
        if index == 1:
            continue
        await send_chunk(
            anon_client,
            token=token,
            upload_id=opened["upload_id"],
            index=index,
            payload=content[index * size : (index + 1) * size],
        )

    status_response = await anon_client.get(
        f"/datasets/uploads/{opened['upload_id']}", headers=auth_headers(token)
    )
    assert status_response.status_code == 200
    assert 1 not in status_response.json()["received"]

    # Completing now must refuse rather than store a truncated archive.
    premature = await anon_client.post(
        f"/datasets/uploads/{opened['upload_id']}/complete",
        headers=auth_headers(token),
    )
    assert premature.status_code == 409
    assert "never arrived" in premature.json()["detail"]

    # Sending only the gap finishes the job -- the point of resuming.
    await send_chunk(
        anon_client,
        token=token,
        upload_id=opened["upload_id"],
        index=1,
        payload=content[size : 2 * size],
    )
    completed = await anon_client.post(
        f"/datasets/uploads/{opened['upload_id']}/complete",
        headers=auth_headers(token),
    )
    assert completed.status_code == 201, completed.text
    assert completed.json()["dataset"]["size_bytes"] == len(content)


# --- Refusals ----------------------------------------------------------------


async def test_a_taken_name_is_refused_before_the_bytes_are_sent(
    anon_client: AsyncClient,
) -> None:
    """The same refusal, delivered before the expensive part rather than after."""
    token = anon_client.admin_token  # type: ignore[attr-defined]
    seeded = await single_shot_upload(anon_client, name="taken", token=token)
    assert seeded.status_code == 201, seeded.text

    opened = await open_session(
        anon_client, token=token, name="taken", total=len(make_archive())
    )
    assert opened.status_code == 409
    assert "already exists" in opened.json()["detail"]


async def test_an_oversized_upload_is_refused_at_the_door(
    anon_client: AsyncClient,
) -> None:
    """The ceiling is checked before any disk is spent, not at assembly.

    A session is storage handed out on nothing more than a declared size, so a
    declaration that could never be completed must not be allowed to buy disk
    first.
    """
    token = anon_client.admin_token  # type: ignore[attr-defined]
    opened = await open_session(anon_client, token=token, total=64 * 1024**3)
    assert opened.status_code == 413
    assert "larger than" in opened.json()["detail"]


async def test_a_chunk_beyond_the_declared_size_is_refused(
    anon_client: AsyncClient,
) -> None:
    """Declaring a small upload must not buy the right to send a large one."""
    token = anon_client.admin_token  # type: ignore[attr-defined]
    opened = (await open_session(anon_client, token=token, total=100)).json()

    response = await send_chunk(
        anon_client,
        token=token,
        upload_id=opened["upload_id"],
        index=0,
        payload=b"x" * 5000,
    )
    assert response.status_code == 422
    assert "already received all the bytes it declared" in response.json()["detail"]


async def test_a_chunk_outside_the_session_is_refused(
    anon_client: AsyncClient,
) -> None:
    token = anon_client.admin_token  # type: ignore[attr-defined]
    content = make_archive()
    opened = (await open_session(anon_client, token=token, total=len(content))).json()

    response = await send_chunk(
        anon_client,
        token=token,
        upload_id=opened["upload_id"],
        index=99,
        payload=b"x",
    )
    assert response.status_code == 422
    assert "outside this upload" in response.json()["detail"]


async def test_a_truncated_transfer_is_refused_rather_than_validated(
    anon_client: AsyncClient,
) -> None:
    """Short bytes must fail as a transfer, not as a confusing zip complaint.

    Handing a truncated archive to the validator would surface a transport
    problem as "that file is not a readable .zip", sending the uploader to
    inspect a file that is perfectly fine on their disk.
    """
    token = anon_client.admin_token  # type: ignore[attr-defined]
    content = make_archive()
    opened = (await open_session(anon_client, token=token, total=len(content))).json()

    await send_chunk(
        anon_client,
        token=token,
        upload_id=opened["upload_id"],
        index=0,
        payload=content[:-10],
    )
    completed = await anon_client.post(
        f"/datasets/uploads/{opened['upload_id']}/complete",
        headers=auth_headers(token),
    )
    assert completed.status_code == 409
    assert "did not arrive intact" in completed.json()["detail"]


async def test_an_invalid_archive_is_refused_the_same_way_either_path(
    anon_client: AsyncClient,
) -> None:
    """Assembly changes how bytes arrive, not what counts as a dataset."""
    token = anon_client.admin_token  # type: ignore[attr-defined]
    content = make_archive(["photos/1.jpg", "photos/2.jpg"])

    response = await upload_in_chunks(anon_client, token=token, content=content)
    assert response.status_code == 422
    assert "no train/<class>/image files" in response.json()["detail"]


async def test_another_admin_cannot_touch_someone_elses_upload(
    anon_client: AsyncClient,
) -> None:
    """A session belonging to someone else answers exactly as a missing one.

    Anything distinguishable would make this an endpoint for discovering what
    other people are uploading and under what names.
    """
    token = anon_client.admin_token  # type: ignore[attr-defined]
    content = make_archive()
    opened = (await open_session(anon_client, token=token, total=len(content))).json()

    other = await another_admin_token()
    response = await anon_client.get(
        f"/datasets/uploads/{opened['upload_id']}", headers=auth_headers(other)
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "unknown upload"


async def test_an_operator_cannot_open_an_upload(api_client: AsyncClient) -> None:
    """Chunking is a transport detail; who may upload is unchanged by it."""
    response = await api_client.post(
        "/datasets/uploads", json={"name": "nope", "total_bytes": 1024}
    )
    assert response.status_code == 403


async def test_abandoning_an_upload_releases_it(anon_client: AsyncClient) -> None:
    token = anon_client.admin_token  # type: ignore[attr-defined]
    content = make_archive()
    opened = (await open_session(anon_client, token=token, total=len(content))).json()

    gone = await anon_client.delete(
        f"/datasets/uploads/{opened['upload_id']}", headers=auth_headers(token)
    )
    assert gone.status_code == 204

    after = await anon_client.get(
        f"/datasets/uploads/{opened['upload_id']}", headers=auth_headers(token)
    )
    assert after.status_code == 404
