# Peers Only Talk To The Orchestrator — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A peer reaches exactly one host — the orchestrator — for both the dataset it trains on and the model it sends back, so object storage needs no route and no credentials outside the control plane.

**Architecture:** Two new JWT audiences (`dataset`, `checkpoint`) extend the existing `node`/`user` separation. The orchestrator gains a streaming dataset endpoint whose URL it signs itself, and a pair of job-scoped checkpoint-blob endpoints. The trainer gets a second `ObjectStore` implementation that speaks HTTP to those endpoints instead of S3. Every peer-facing URL is derived from the request's own `Host` header, so the same build works on a LAN, a VPN, or a public tunnel with no configuration.

**Tech Stack:** FastAPI, PyJWT (HS256), boto3/MinIO, SQLAlchemy 2.0 async, pytest/httpx, React 19 + TanStack Query.

**Spec:** `docs/superpowers/specs/2026-09-09-peers-only-talk-to-the-orchestrator-design.md`

## Global Constraints

- Signing algorithm is HS256 with `settings.jwt_signing_key`; every audience is validated explicitly. Never accept a token whose `aud` you did not demand.
- Every JWT decode must require `["exp", "sub", "aud"]`, matching `decode_node_jwt`.
- No fabricated values: an absent or unreadable input is an error or a `None`, never a convenient default. (CONTRIBUTING.md rules 1–3.)
- Peer-facing URLs are always derived from the request, never from configuration.
- Streaming endpoints must not buffer a whole object in memory — use `iter_object`.
- Tests use the real Postgres via `TEST_DATABASE_URL`; object storage is stubbed, following `tests/test_datasets.py`.
- Docstrings explain *why*, in the voice of the surrounding code. No comment restates what the line does.

---

### Task 1: Dataset and checkpoint token audiences

**Files:**
- Modify: `orchestrator/core/security.py`
- Modify: `orchestrator/core/config.py`
- Test: `tests/test_scoped_tokens.py` (create)

**Interfaces:**
- Consumes: `NODE_AUDIENCE`, `USER_AUDIENCE`, `_JWT_ALGORITHM`, `JWTValidationError` (existing).
- Produces:
  - `DATASET_AUDIENCE: str = "dataset"`, `CHECKPOINT_AUDIENCE: str = "checkpoint"`
  - `create_dataset_jwt(*, dataset_id: str, signing_key: str, ttl_seconds: int, now: datetime | None = None) -> str`
  - `decode_dataset_jwt(token: str, *, signing_key: str) -> str` → dataset id
  - `create_checkpoint_jwt(*, job_id: str, signing_key: str, ttl_seconds: int, now: datetime | None = None) -> str`
  - `decode_checkpoint_jwt(token: str, *, signing_key: str) -> str` → job id
  - `settings.dataset_token_ttl_seconds: int = 3600`
  - `settings.checkpoint_token_ttl_seconds: int = 86400`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scoped_tokens.py`:

```python
"""Dataset and checkpoint tokens are scoped to one object and one audience.

These two audiences exist so a peer can fetch its dataset and store its model
without holding object-store credentials. The whole security value is in the
narrowness: a token names one dataset or one job, and a token minted for one
audience is inert everywhere else. Each of those claims gets a test, because a
scoped credential that is silently broad is worse than no scoping at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.core.security import (
    JWTValidationError,
    create_checkpoint_jwt,
    create_dataset_jwt,
    create_node_jwt,
    decode_checkpoint_jwt,
    decode_dataset_jwt,
)

_KEY = "test-signing-key"


def test_dataset_token_round_trips() -> None:
    token = create_dataset_jwt(dataset_id="ds-1", signing_key=_KEY, ttl_seconds=60)
    assert decode_dataset_jwt(token, signing_key=_KEY) == "ds-1"


def test_checkpoint_token_round_trips() -> None:
    token = create_checkpoint_jwt(job_id="job-1", signing_key=_KEY, ttl_seconds=60)
    assert decode_checkpoint_jwt(token, signing_key=_KEY) == "job-1"


def test_dataset_token_is_not_a_checkpoint_token() -> None:
    """Audience separation is the boundary; a valid signature is not enough."""
    token = create_dataset_jwt(dataset_id="ds-1", signing_key=_KEY, ttl_seconds=60)
    with pytest.raises(JWTValidationError):
        decode_checkpoint_jwt(token, signing_key=_KEY)


def test_node_token_cannot_fetch_a_dataset() -> None:
    token = create_node_jwt(node_id="n-1", signing_key=_KEY, ttl_seconds=60)
    with pytest.raises(JWTValidationError):
        decode_dataset_jwt(token, signing_key=_KEY)


def test_expired_dataset_token_is_rejected() -> None:
    past = datetime.now(UTC) - timedelta(hours=2)
    token = create_dataset_jwt(
        dataset_id="ds-1", signing_key=_KEY, ttl_seconds=60, now=past
    )
    with pytest.raises(JWTValidationError):
        decode_dataset_jwt(token, signing_key=_KEY)


def test_wrong_key_is_rejected() -> None:
    token = create_dataset_jwt(dataset_id="ds-1", signing_key=_KEY, ttl_seconds=60)
    with pytest.raises(JWTValidationError):
        decode_dataset_jwt(token, signing_key="a-different-key")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_scoped_tokens.py -v`
Expected: FAIL — `ImportError: cannot import name 'create_dataset_jwt'`

- [ ] **Step 3: Add the audiences and helpers**

In `orchestrator/core/security.py`, below `USER_AUDIENCE`:

```python
#: JWT audience for a URL that fetches one dataset archive. The trainer
#: downloads with a bare urlopen and sends no headers, so the proof has to
#: travel in the URL itself — the same reason a presigned URL exists, except
#: signed by us, for our own host, so it stays valid wherever the peer reached
#: us from.
DATASET_AUDIENCE = "dataset"

#: JWT audience for a peer writing one job's checkpoints back through the
#: control plane. Scoped to a single job so the credential a peer holds can
#: touch that job's blobs and nothing else in the bucket.
CHECKPOINT_AUDIENCE = "checkpoint"
```

Then add, following the shape of `create_node_jwt`/`decode_node_jwt`:

```python
def _create_scoped_jwt(
    *, subject: str, audience: str, signing_key: str, ttl_seconds: int,
    now: datetime | None = None,
) -> str:
    issued = now or datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "aud": audience,
        "iat": int(issued.timestamp()),
        "exp": int((issued + timedelta(seconds=ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, signing_key, algorithm=_JWT_ALGORITHM)


def _decode_scoped_jwt(token: str, *, audience: str, signing_key: str) -> str:
    try:
        payload = jwt.decode(
            token,
            signing_key,
            algorithms=[_JWT_ALGORITHM],
            audience=audience,
            options={"require": ["exp", "sub", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise JWTValidationError(str(exc)) from exc
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise JWTValidationError("token has no usable subject")
    return subject


def create_dataset_jwt(
    *, dataset_id: str, signing_key: str, ttl_seconds: int, now: datetime | None = None
) -> str:
    """Issue a URL-embeddable token granting a read of one dataset archive."""
    return _create_scoped_jwt(
        subject=dataset_id, audience=DATASET_AUDIENCE,
        signing_key=signing_key, ttl_seconds=ttl_seconds, now=now,
    )


def decode_dataset_jwt(token: str, *, signing_key: str) -> str:
    """Verify a dataset token; return the dataset id it names."""
    return _decode_scoped_jwt(token, audience=DATASET_AUDIENCE, signing_key=signing_key)


def create_checkpoint_jwt(
    *, job_id: str, signing_key: str, ttl_seconds: int, now: datetime | None = None
) -> str:
    """Issue a token granting read/write of one job's checkpoint blobs."""
    return _create_scoped_jwt(
        subject=job_id, audience=CHECKPOINT_AUDIENCE,
        signing_key=signing_key, ttl_seconds=ttl_seconds, now=now,
    )


def decode_checkpoint_jwt(token: str, *, signing_key: str) -> str:
    """Verify a checkpoint token; return the job id it is scoped to."""
    return _decode_scoped_jwt(
        token, audience=CHECKPOINT_AUDIENCE, signing_key=signing_key
    )
```

In `orchestrator/core/config.py`, beside `dataset_url_ttl_seconds`:

```python
    # Lifetime of the dataset-fetch token embedded in the URL a peer is handed
    # at claim time. Matches the old presigned-URL TTL: long enough to download
    # a large archive on a slow link, short enough that a finished peer cannot
    # keep reading.
    dataset_token_ttl_seconds: int = 3600
    # Lifetime of the checkpoint token the trainer writes with. Long by JWT
    # standards, and deliberately: a training run outlives the 15-minute access
    # token, and the trainer is a subprocess whose environment is fixed at
    # launch, so it cannot refresh. Scoped to one job's blobs, so the blast
    # radius is that job and nothing else.
    checkpoint_token_ttl_seconds: int = 86400
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_scoped_tokens.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add orchestrator/core/security.py orchestrator/core/config.py tests/test_scoped_tokens.py
git commit -m "feat(auth): dataset and checkpoint token audiences"
```

---

### Task 2: One shared "where did this peer reach us" helper

**Files:**
- Create: `orchestrator/api/public_url.py`
- Modify: `orchestrator/api/installer.py` (delete `_public_base_url`, import instead)
- Test: `tests/test_public_url.py` (create)

**Interfaces:**
- Produces: `public_base_url(request: Request) -> str` — scheme + host, no trailing slash.

Two callers now need this (the installer and the claim path), so it moves rather than being copied.

- [ ] **Step 1: Write the failing test**

Create `tests/test_public_url.py`:

```python
"""The address a peer is handed is the one it actually reached us on.

Every peer-facing URL in this system is built from the request rather than
configuration, because configuration drifts from reality and a request cannot.
A LAN IP, a Tailscale address and a tunnel hostname all have to work with no
operator action, and a proxy that terminates TLS in front of us must not cause
us to hand out an http:// URL.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.requests import Request

from orchestrator.api.public_url import public_base_url


def _request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": "/",
        "headers": Headers(headers).raw,
    }
    return Request(scope)


def test_uses_the_host_header() -> None:
    assert public_base_url(_request({"host": "192.168.1.5:8090"})) == (
        "http://192.168.1.5:8090"
    )


def test_forwarded_host_wins_over_host() -> None:
    url = public_base_url(
        _request({"host": "localhost:8090", "x-forwarded-host": "abc.trycloudflare.com"})
    )
    assert url == "http://abc.trycloudflare.com"


def test_forwarded_proto_is_honoured() -> None:
    """A tunnel terminates TLS in front of us, so request.scheme says http."""
    url = public_base_url(
        _request({"host": "abc.trycloudflare.com", "x-forwarded-proto": "https"})
    )
    assert url == "https://abc.trycloudflare.com"


def test_no_trailing_slash() -> None:
    assert not public_base_url(_request({"host": "h:1"})).endswith("/")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_public_url.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestrator.api.public_url'`

- [ ] **Step 3: Create the module and repoint the installer**

Create `orchestrator/api/public_url.py`, moving the body of
`installer._public_base_url` verbatim (keep its docstring — it explains the
`X-Forwarded-*` reasoning) and renaming it to `public_base_url`.

In `orchestrator/api/installer.py`, delete the old function and add
`from orchestrator.api.public_url import public_base_url`, then update its two
call sites.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_public_url.py tests/test_installer.py -v`
Expected: all pass — the installer's behaviour is unchanged.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/api/public_url.py orchestrator/api/installer.py tests/test_public_url.py
git commit -m "refactor(api): share the request-derived base URL helper"
```

---

### Task 3: The orchestrator serves dataset archives

**Files:**
- Modify: `orchestrator/api/datasets.py`
- Modify: `orchestrator/api/leases.py:86-146` (`_attach_dataset_fetch`), and `claim_lease` gains `request: Request`
- Test: `tests/test_dataset_streaming.py` (create)

**Interfaces:**
- Consumes: `create_dataset_jwt`, `decode_dataset_jwt`, `public_base_url`, `DatasetObjectStore.iter_object`.
- Produces: `GET /datasets/{dataset_id}/archive?token=…` → `StreamingResponse`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_dataset_streaming.py`:

```python
"""A peer downloads its dataset from the orchestrator, not from storage.

The URL a peer receives is signed by us and served by us, so it is valid from
wherever that peer reached us — which is the whole point: a presigned storage
URL is signed for one host, and that host is unresolvable the moment the peer
is not inside our network.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from orchestrator.core.security import create_dataset_jwt

_KEY = "dev-only-change-me"


@pytest.mark.asyncio
async def test_archive_streams_with_a_valid_token(
    client: AsyncClient, uploaded_dataset: dict
) -> None:
    token = create_dataset_jwt(
        dataset_id=uploaded_dataset["id"], signing_key=_KEY, ttl_seconds=60
    )
    resp = await client.get(
        f"/datasets/{uploaded_dataset['id']}/archive", params={"token": token}
    )
    assert resp.status_code == 200
    assert resp.content == uploaded_dataset["bytes"]


@pytest.mark.asyncio
async def test_a_token_for_one_dataset_cannot_read_another(
    client: AsyncClient, uploaded_dataset: dict
) -> None:
    other = create_dataset_jwt(
        dataset_id=str(uuid.uuid4()), signing_key=_KEY, ttl_seconds=60
    )
    resp = await client.get(
        f"/datasets/{uploaded_dataset['id']}/archive", params={"token": other}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_missing_token_is_refused(
    client: AsyncClient, uploaded_dataset: dict
) -> None:
    resp = await client.get(f"/datasets/{uploaded_dataset['id']}/archive")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_claim_hands_out_an_orchestrator_url(
    client: AsyncClient, claimed_spec: dict
) -> None:
    """The peer must never be given a storage hostname."""
    url = claimed_spec["dataset_url"]
    assert "/datasets/" in url and "token=" in url
    assert "minio" not in url and ":9000" not in url


@pytest.mark.asyncio
async def test_claim_url_tracks_the_request_host(
    client: AsyncClient, claim_with_host
) -> None:
    spec = await claim_with_host("10.1.2.3:8090")
    assert spec["dataset_url"].startswith("http://10.1.2.3:8090/datasets/")
```

Add the `uploaded_dataset`, `claimed_spec` and `claim_with_host` fixtures to
`tests/conftest.py`, built with the `StubObjectStore` pattern already used in
`tests/test_datasets.py` — reuse that stub rather than writing a second one.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_dataset_streaming.py -v`
Expected: FAIL — 404 on the archive route; `dataset_url` still contains `minio`.

- [ ] **Step 3: Add the endpoint**

In `orchestrator/api/datasets.py`:

```python
@router.get("/{dataset_id}/archive")
async def download_dataset_archive(
    dataset_id: uuid.UUID,
    token: str,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> StreamingResponse:
    """Stream one dataset archive to the peer that was granted it.

    Authorised by a token in the query string rather than a header, because the
    trainer fetches with a bare ``urlopen`` and sends none. The token names the
    dataset, and the name is checked against the path, so a token cannot be
    pointed at a different archive.

    Streamed in chunks: an archive may be gigabytes, and holding one in the
    orchestrator's memory would make a large upload a denial of service against
    every other request.
    """
    try:
        granted = decode_dataset_jwt(token, signing_key=settings.jwt_signing_key)
    except JWTValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="invalid or expired token"
        ) from exc
    if granted != str(dataset_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this token is for a different dataset",
        )

    dataset = await get_dataset(session, dataset_id=dataset_id)
    if dataset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown dataset")

    store = DatasetObjectStore(settings)
    return StreamingResponse(
        store.iter_object(key=dataset.object_key),
        media_type="application/zip",
        headers={"content-disposition": f'attachment; filename="{dataset.name}.zip"'},
    )
```

- [ ] **Step 4: Point the claim path at it**

In `orchestrator/api/leases.py`, give `_attach_dataset_fetch` a `base_url: str`
parameter and replace the presigning block:

```python
    job_spec["dataset_url"] = (
        f"{base_url}/datasets/{dataset.id}/archive"
        f"?token={create_dataset_jwt(
            dataset_id=str(dataset.id),
            signing_key=settings.jwt_signing_key,
            ttl_seconds=settings.dataset_token_ttl_seconds,
        )}"
    )
```

Delete the now-unused `ObjectStoreError` handling around it and the
`DatasetObjectStore` import if nothing else in the module uses it.

Add `request: Request` to `claim_lease` and pass
`base_url=public_base_url(request)` through at line 164.

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_dataset_streaming.py tests/test_datasets.py -v`
Expected: all pass. Update any assertion in `test_datasets.py` that expected a
presigned URL — that expectation is now wrong by design.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/api/datasets.py orchestrator/api/leases.py tests/
git commit -m "feat(datasets): serve archives from the orchestrator, not storage"
```

---

### Task 4: Job-scoped checkpoint blob endpoints

**Files:**
- Modify: `orchestrator/api/jobs.py`
- Modify: `orchestrator/services/object_store.py` (`CheckpointObjectStore` gains `put_bytes`)
- Test: `tests/test_checkpoint_blobs.py` (create)

**Interfaces:**
- Produces:
  - `PUT /jobs/{job_id}/checkpoint-blobs/{key:path}` — bearer checkpoint token, body is raw bytes, 204.
  - `GET /jobs/{job_id}/checkpoint-blobs/{key:path}` — same auth, streams bytes, 404 when absent.
  - `CheckpointObjectStore.put_bytes(*, key: str, data: bytes) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_checkpoint_blobs.py`:

```python
"""A peer stores its model through the control plane, holding no S3 keys.

The authorisation rule under test is narrow on purpose: a checkpoint token
names one job, and every key it may touch begins with that job's prefix. Both
halves matter — without the prefix check, a token for a cheap job would be a
skeleton key for the whole bucket.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from orchestrator.core.security import create_checkpoint_jwt

_KEY = "dev-only-change-me"


def _auth(job_id: str) -> dict[str, str]:
    token = create_checkpoint_jwt(job_id=job_id, signing_key=_KEY, ttl_seconds=60)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_put_then_get_round_trips(client: AsyncClient, queued_job: dict) -> None:
    job_id = queued_job["id"]
    key = f"checkpoints/{job_id}/manifest.json"
    put = await client.put(
        f"/jobs/{job_id}/checkpoint-blobs/{key}",
        content=b'{"entries": []}',
        headers=_auth(job_id),
    )
    assert put.status_code == 204

    got = await client.get(
        f"/jobs/{job_id}/checkpoint-blobs/{key}", headers=_auth(job_id)
    )
    assert got.status_code == 200
    assert got.content == b'{"entries": []}'


@pytest.mark.asyncio
async def test_a_token_cannot_write_another_jobs_blobs(
    client: AsyncClient, queued_job: dict
) -> None:
    other_job = str(uuid.uuid4())
    key = f"checkpoints/{queued_job['id']}/manifest.json"
    resp = await client.put(
        f"/jobs/{queued_job['id']}/checkpoint-blobs/{key}",
        content=b"x",
        headers=_auth(other_job),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_a_key_outside_the_job_prefix_is_refused(
    client: AsyncClient, queued_job: dict
) -> None:
    """The token is valid; the key is not one this job owns."""
    job_id = queued_job["id"]
    resp = await client.put(
        f"/jobs/{job_id}/checkpoint-blobs/checkpoints/{uuid.uuid4()}/manifest.json",
        content=b"x",
        headers=_auth(job_id),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_traversal_in_the_key_is_refused(
    client: AsyncClient, queued_job: dict
) -> None:
    job_id = queued_job["id"]
    resp = await client.put(
        f"/jobs/{job_id}/checkpoint-blobs/checkpoints/{job_id}/../../secrets",
        content=b"x",
        headers=_auth(job_id),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_a_user_token_is_not_a_checkpoint_token(
    client: AsyncClient, queued_job: dict, auth_headers: dict
) -> None:
    job_id = queued_job["id"]
    resp = await client.get(
        f"/jobs/{job_id}/checkpoint-blobs/checkpoints/{job_id}/manifest.json",
        headers=auth_headers,
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_missing_blob_is_404(client: AsyncClient, queued_job: dict) -> None:
    job_id = queued_job["id"]
    resp = await client.get(
        f"/jobs/{job_id}/checkpoint-blobs/checkpoints/{job_id}/nope.pt",
        headers=_auth(job_id),
    )
    assert resp.status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_checkpoint_blobs.py -v`
Expected: FAIL — 404, the routes do not exist.

- [ ] **Step 3: Add `put_bytes` to the checkpoint store**

In `orchestrator/services/object_store.py`, on `_BucketStore` beside `get_bytes`:

```python
    def put_bytes(self, *, key: str, data: bytes) -> None:
        """Write an object. Raises ObjectStoreError on any storage failure."""
        try:
            self._get_client().put_object(Bucket=self._bucket, Key=key, Body=data)
        except Exception as exc:  # noqa: BLE001 - surfaced as one storage error
            raise ObjectStoreError(f"could not write {key}") from exc
```

Update `CheckpointObjectStore`'s docstring: it is no longer read-only from the
orchestrator's side, because the peer's writes now arrive through it.

- [ ] **Step 4: Add the endpoints**

In `orchestrator/api/jobs.py`:

```python
def _authorize_blob(
    *, job_id: uuid.UUID, key: str, authorization: str | None, settings: Settings
) -> None:
    """Prove the caller holds a checkpoint token for exactly this job and key.

    Two independent checks. The token's subject must be this job — otherwise
    any peer could write into any job. And the key must sit under this job's
    prefix — otherwise a token for one job would reach the whole bucket, which
    is the very thing keeping S3 credentials off peers was meant to prevent.
    """
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token"
        )
    try:
        granted = decode_checkpoint_jwt(
            authorization[len("Bearer ") :].strip(),
            signing_key=settings.jwt_signing_key,
        )
    except JWTValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="invalid or expired token"
        ) from exc
    if granted != str(job_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this token is for a different job",
        )
    # posixpath.normpath collapses any ".." before the prefix test, so a key
    # that escapes by traversal is caught rather than merely looking correct.
    normalized = posixpath.normpath(key)
    if normalized != key or not normalized.startswith(f"checkpoints/{job_id}/"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="key is outside this job's checkpoint prefix",
        )


@router.put(
    "/{job_id}/checkpoint-blobs/{key:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
async def put_checkpoint_blob(
    job_id: uuid.UUID,
    key: str,
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    """Store one checkpoint object on behalf of the peer training this job."""
    _authorize_blob(
        job_id=job_id, key=key, authorization=authorization, settings=settings
    )
    CheckpointObjectStore(settings).put_bytes(key=key, data=await request.body())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{job_id}/checkpoint-blobs/{key:path}")
async def get_checkpoint_blob(
    job_id: uuid.UUID,
    key: str,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings_dep),
) -> StreamingResponse:
    """Read back one checkpoint object — how a reassigned attempt resumes."""
    _authorize_blob(
        job_id=job_id, key=key, authorization=authorization, settings=settings
    )
    store = CheckpointObjectStore(settings)
    if store.head_size_bytes(key=key) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such blob")
    return StreamingResponse(
        store.iter_object(key=key), media_type="application/octet-stream"
    )
```

Import `posixpath`, `Header`, `Request`, `Response`, `decode_checkpoint_jwt`,
`JWTValidationError`, `CheckpointObjectStore` at the top of the module.

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_checkpoint_blobs.py -v`
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add orchestrator/api/jobs.py orchestrator/services/object_store.py tests/test_checkpoint_blobs.py
git commit -m "feat(jobs): job-scoped checkpoint blob storage through the control plane"
```

---

### Task 5: The trainer's HTTP-backed object store

**Files:**
- Modify: `trainer/checkpoint.py`
- Modify: `trainer/train.py:599` (use `store_from_env`)
- Test: `tests/test_http_object_store.py` (create)

**Interfaces:**
- Produces:
  - `class HttpObjectStore` with `put_bytes(key, data)` / `get_bytes(key)`, matching the existing `ObjectStore` Protocol.
  - `HttpObjectStore.from_env(env: dict[str, str]) -> HttpObjectStore | None`
  - `store_from_env(env: dict[str, str]) -> ObjectStore | None`
- Reads env: `CHECKPOINT_UPLOAD_URL` (the orchestrator base), `CHECKPOINT_UPLOAD_TOKEN`, `JOB_ID`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_http_object_store.py`:

```python
"""The trainer's store speaks HTTP to the orchestrator instead of S3.

Same two-method Protocol as the S3 store, so nothing above it changes: this is
a new implementation of an existing seam. The selection tests matter as much as
the transport ones — a co-located dev setup must keep working, and a peer with
neither configuration must still train, silently, without checkpointing.
"""

from __future__ import annotations

import pytest

from trainer.checkpoint import (
    HttpObjectStore,
    ObjectNotFoundError,
    S3ObjectStore,
    store_from_env,
)

_ENV = {
    "CHECKPOINT_UPLOAD_URL": "http://orch:8090",
    "CHECKPOINT_UPLOAD_TOKEN": "tok",
    "JOB_ID": "job-1",
}


def test_from_env_needs_every_field() -> None:
    assert HttpObjectStore.from_env({}) is None
    assert HttpObjectStore.from_env({"CHECKPOINT_UPLOAD_URL": "http://x"}) is None
    assert HttpObjectStore.from_env(_ENV) is not None


def test_put_and_get_round_trip(http_blob_server) -> None:
    """Against a real local HTTP server, not a mock: the bug this replaces was
    in transport, and a mock would have been just as green as the broken code."""
    store = HttpObjectStore.from_env(
        {**_ENV, "CHECKPOINT_UPLOAD_URL": http_blob_server.base_url}
    )
    store.put_bytes("checkpoints/job-1/manifest.json", b"hello")
    assert store.get_bytes("checkpoints/job-1/manifest.json") == b"hello"


def test_missing_key_raises_object_not_found(http_blob_server) -> None:
    store = HttpObjectStore.from_env(
        {**_ENV, "CHECKPOINT_UPLOAD_URL": http_blob_server.base_url}
    )
    with pytest.raises(ObjectNotFoundError):
        store.get_bytes("checkpoints/job-1/absent.pt")


def test_store_from_env_prefers_http() -> None:
    env = {
        **_ENV,
        "S3_ENDPOINT_URL": "http://minio:9000",
        "S3_ACCESS_KEY": "a",
        "S3_SECRET_KEY": "b",
        "S3_BUCKET_CHECKPOINTS": "checkpoints",
    }
    assert isinstance(store_from_env(env), HttpObjectStore)


def test_store_from_env_falls_back_to_s3() -> None:
    env = {
        "S3_ENDPOINT_URL": "http://minio:9000",
        "S3_ACCESS_KEY": "a",
        "S3_SECRET_KEY": "b",
        "S3_BUCKET_CHECKPOINTS": "checkpoints",
    }
    assert isinstance(store_from_env(env), S3ObjectStore)


def test_store_from_env_returns_none_when_unconfigured() -> None:
    assert store_from_env({}) is None
```

Add an `http_blob_server` fixture to `tests/conftest.py`: a
`http.server.ThreadingHTTPServer` on port 0 holding a dict, answering `PUT`
with 204 and `GET` with the stored bytes or 404, exposing `base_url`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_http_object_store.py -v`
Expected: FAIL — `ImportError: cannot import name 'HttpObjectStore'`

- [ ] **Step 3: Implement the store**

In `trainer/checkpoint.py`:

```python
class HttpObjectStore:
    """An ObjectStore backed by the orchestrator rather than S3 directly.

    A peer holds no storage credentials: it holds a token scoped to one job,
    and the control plane does the writing. That removes both the credential
    distribution problem and the routing one — the orchestrator is the single
    host a peer already has to reach, so no extra port has to be open or even
    resolvable from wherever the peer is.
    """

    def __init__(self, *, base_url: str, token: str, job_id: str) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._job_id = job_id

    def _url(self, key: str) -> str:
        return f"{self._base}/jobs/{self._job_id}/checkpoint-blobs/{key}"

    def put_bytes(self, key: str, data: bytes) -> None:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            self._url(key), data=data, method="PUT",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/octet-stream",
            },
        )
        try:
            with urllib.request.urlopen(request):
                pass
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"could not store {key}: HTTP {exc.code} {exc.reason}"
            ) from exc

    def get_bytes(self, key: str) -> bytes:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            self._url(key), headers={"Authorization": f"Bearer {self._token}"}
        )
        try:
            with urllib.request.urlopen(request) as response:
                body: bytes = response.read()
                return body
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ObjectNotFoundError(key) from exc
            raise

    @classmethod
    def from_env(cls, env: dict[str, str]) -> HttpObjectStore | None:
        base = env.get("CHECKPOINT_UPLOAD_URL")
        token = env.get("CHECKPOINT_UPLOAD_TOKEN")
        job_id = env.get("JOB_ID")
        if not (base and token and job_id):
            return None
        return cls(base_url=base, token=token, job_id=job_id)


def store_from_env(env: dict[str, str]) -> ObjectStore | None:
    """The configured checkpoint store, or None when there is none.

    HTTP wins over S3 when both are present: a peer that was handed a scoped
    token should use it rather than any ambient credentials it happens to have,
    and the co-located dev path keeps working through the S3 fallback.
    """
    return HttpObjectStore.from_env(env) or S3ObjectStore.from_env(env)
```

In `trainer/train.py`, replace line 599:

```python
    checkpoint_store = ckpt.store_from_env(dict(os.environ))
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_http_object_store.py tests/test_checkpoint_manifest.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add trainer/checkpoint.py trainer/train.py tests/test_http_object_store.py tests/conftest.py
git commit -m "feat(trainer): store checkpoints through the orchestrator"
```

---

### Task 6: The agent hands the trainer a scoped token

**Files:**
- Modify: `orchestrator/schemas/lease.py` (spec carries `checkpoint_upload_token`)
- Modify: `orchestrator/api/leases.py` (mint it at claim time)
- Modify: `agent/main.py` (`TrainerLaunchConfig` gains the two fields)
- Modify: `agent/runtime/docker_launcher.py:212-222`, `agent/runtime/subprocess_launcher.py`
- Test: `tests/test_checkpoint_token_delivery.py` (create)

**Interfaces:**
- Consumes: `create_checkpoint_jwt`, `public_base_url`, `settings.checkpoint_token_ttl_seconds`.
- Produces: `job_spec["checkpoint_upload_token"]`, `job_spec["checkpoint_upload_url"]`; trainer env `CHECKPOINT_UPLOAD_URL`, `CHECKPOINT_UPLOAD_TOKEN`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_checkpoint_token_delivery.py`:

```python
"""Claiming a job hands the peer everything it needs to return a model.

Before this, a peer could train and had nowhere to put the result unless an
operator had separately given it S3 credentials — so the common outcome was a
finished job with no downloadable model and no error explaining why.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from orchestrator.core.security import decode_checkpoint_jwt

_KEY = "dev-only-change-me"


@pytest.mark.asyncio
async def test_claim_includes_a_checkpoint_token(claimed_spec: dict) -> None:
    token = claimed_spec["checkpoint_upload_token"]
    assert decode_checkpoint_jwt(token, signing_key=_KEY) == claimed_spec["job_id"]


@pytest.mark.asyncio
async def test_claim_includes_the_orchestrator_url(claim_with_host) -> None:
    spec = await claim_with_host("10.1.2.3:8090")
    assert spec["checkpoint_upload_url"] == "http://10.1.2.3:8090"


@pytest.mark.asyncio
async def test_no_storage_credentials_are_ever_sent(claimed_spec: dict) -> None:
    """The point of the whole change: peers get tokens, never keys."""
    body = str(claimed_spec)
    assert "S3_ACCESS_KEY" not in body and "minioadmin" not in body
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_checkpoint_token_delivery.py -v`
Expected: FAIL — `KeyError: 'checkpoint_upload_token'`

- [ ] **Step 3: Mint the token at claim time**

In `_attach_dataset_fetch`'s caller in `orchestrator/api/leases.py`, after the
spec is assembled (this applies to every job, not only ones with a dataset, so
it does not belong inside `_attach_dataset_fetch`):

```python
    job_spec["checkpoint_upload_url"] = base_url
    job_spec["checkpoint_upload_token"] = create_checkpoint_jwt(
        job_id=str(job.id),
        signing_key=settings.jwt_signing_key,
        ttl_seconds=settings.checkpoint_token_ttl_seconds,
    )
```

- [ ] **Step 4: Pass it through both launchers**

In `agent/main.py`, add to `TrainerLaunchConfig`:

```python
    checkpoint_upload_url: str | None = None
    checkpoint_upload_token: str | None = None
```

and populate them from the claimed spec where the launch config is built.

In `agent/runtime/docker_launcher.py`, alongside the existing S3 block:

```python
    if config.checkpoint_upload_url and config.checkpoint_upload_token:
        environment["CHECKPOINT_UPLOAD_URL"] = config.checkpoint_upload_url
        environment["CHECKPOINT_UPLOAD_TOKEN"] = config.checkpoint_upload_token
        environment["CHECKPOINT_EVERY_N_STEPS"] = str(config.checkpoint_every_n_steps)
```

Apply the identical block in `agent/runtime/subprocess_launcher.py` — the
native path is the one a peer without Docker actually takes, and omitting it
there is exactly how this failed before.

Update the log line so an operator can tell which store is in use:

```python
        if config.checkpoint_upload_token:
            logger.info(
                "checkpoints go through the orchestrator at %s",
                config.checkpoint_upload_url,
            )
        elif launch_config.checkpointing_enabled():
            ...
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_checkpoint_token_delivery.py tests/ -k "agent or lease" -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add orchestrator/ agent/ tests/test_checkpoint_token_delivery.py
git commit -m "feat(agent): hand the trainer a job-scoped checkpoint token"
```

---

### Task 7: The session stops expiring mid-upload

**Files:**
- Modify: `orchestrator/api/auth.py`
- Modify: `dashboard/src/api/client.ts`
- Test: `tests/test_token_renewal.py` (create)

**Interfaces:**
- Produces: `POST /auth/token/renew` → `{"access_token": str, "expires_in": int}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_token_renewal.py`:

```python
"""A live session can extend itself; a dead one cannot.

Fifteen minutes is short enough that fetching a dataset off another machine
outlasts it, and the failure surfaced as "invalid or expired token" on upload
— which reads like a bug in the upload. Renewal removes that without widening
anything: only an already-valid token is accepted.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_renew_returns_a_fresh_token(
    client: AsyncClient, auth_headers: dict
) -> None:
    resp = await client.post("/auth/token/renew", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] and body["expires_in"] > 0


@pytest.mark.asyncio
async def test_the_renewed_token_works(client: AsyncClient, auth_headers: dict) -> None:
    fresh = (await client.post("/auth/token/renew", headers=auth_headers)).json()
    resp = await client.get(
        "/jobs", headers={"Authorization": f"Bearer {fresh['access_token']}"}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_renew_without_a_token_is_401(client: AsyncClient) -> None:
    assert (await client.post("/auth/token/renew")).status_code == 401


@pytest.mark.asyncio
async def test_a_node_token_cannot_renew_a_user_session(
    client: AsyncClient, node_auth_headers: dict
) -> None:
    resp = await client.post("/auth/token/renew", headers=node_auth_headers)
    assert resp.status_code in (401, 403)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_token_renewal.py -v`
Expected: FAIL — 404, the route does not exist.

- [ ] **Step 3: Add the endpoint**

In `orchestrator/api/auth.py`:

```python
@router.post("/token/renew", response_model=TokenResponse)
async def renew_user_token(
    user: User = Depends(require_user_auth),
    settings: Settings = Depends(get_settings_dep),
) -> TokenResponse:
    """Exchange a still-valid user token for a fresh one.

    Only a live session renews: an expired token fails ``require_user_auth``
    before reaching here, so this shortens no security property. It exists
    because a fifteen-minute window is easy to cross between two clicks — most
    reliably while fetching a dataset off another machine — and the failure
    then appears as a broken upload rather than a lapsed sign-in.

    The role is re-read from the live row rather than copied from the old
    token, so a downgrade cannot be renewed indefinitely.
    """
    return TokenResponse(
        access_token=create_user_jwt(
            user_id=str(user.id),
            username=user.username,
            role=user.role.value,
            signing_key=settings.jwt_signing_key,
            ttl_seconds=settings.user_access_token_ttl_seconds,
        ),
        expires_in=settings.user_access_token_ttl_seconds,
    )
```

- [ ] **Step 4: Renew from the dashboard**

In `dashboard/src/api/client.ts`, start a timer on sign-in that calls
`POST /api/auth/token/renew` every `expires_in / 2` seconds, replaces the
stored token on success, and stops on failure or sign-out. Half the TTL means a
single failed renewal is retried once before the session can actually lapse.

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_token_renewal.py -v && cd dashboard && npx tsc -b && npx eslint src --max-warnings=0`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add orchestrator/api/auth.py dashboard/src/api/client.ts tests/test_token_renewal.py
git commit -m "feat(auth): renew a live session instead of dropping it"
```

---

### Task 8: Reachable from anywhere, and the stale guidance removed

**Files:**
- Create: `deploy/tunnel.md`
- Create: `deploy/Caddyfile`
- Create: `deploy/compose.public.yaml`
- Modify: `deploy/.env.example` (delete the `S3_ENDPOINT_URL` peer guidance)
- Modify: `deploy/compose.yaml` (revert `S3_ENDPOINT_URL` to the in-network value)
- Modify: `docs/STATUS.md`

**Interfaces:** none — configuration and documentation.

This is the task where the workaround gets deleted. Leaving it would instruct a
future operator to maintain a value nothing reads.

- [ ] **Step 1: Verify nothing peer-side reads the endpoint any more**

Run: `grep -rn "S3_ENDPOINT_URL" agent/ trainer/ installer/`
Expected: only `trainer/checkpoint.py`'s `S3ObjectStore.from_env` fallback.
Anything else is a missed call site — fix it before continuing.

- [ ] **Step 2: Revert the compose default and delete the guidance**

In `deploy/compose.yaml` restore `S3_ENDPOINT_URL: http://minio:9000` — with
the orchestrator the only reader, the in-network name is now simply correct.
Delete the peer-facing block added to `deploy/.env.example`, replacing it with
one line noting the value is orchestrator-internal.

- [ ] **Step 3: Write `deploy/tunnel.md`**

Cover: installing `cloudflared`; `cloudflared tunnel --url http://localhost:5173`;
setting `VITE_ALLOWED_HOSTS` to the tunnel hostname **before** starting Vite
(Vite rejects unknown `Host` headers, and that failure looks like a tunnel
problem); that peers install from `https://<host>/install.ps1` with the address
baked in from the `Host` header; and plainly, that quick tunnels are ephemeral
and rate-limited — a demo tool, not a deployment. State that `world_size > 1`
needs Tailscale because ranks dial each other directly.

- [ ] **Step 4: Write the production config**

`deploy/Caddyfile` — TLS via Let's Encrypt, serving the built dashboard as
static files and reverse-proxying `/api` to the orchestrator.
`deploy/compose.public.yaml` — an overlay adding Caddy, requiring `APP_ENV`,
`JWT_SIGNING_KEY` and `ADMIN_API_KEY` with no defaults, so the existing
`_enforce_production_secrets` check does its job.

- [ ] **Step 5: Update STATUS.md**

Move "a peer needs a route to object storage" out of the gaps. Add the two that
remain honest: no TLS on a LAN, and `world_size > 1` across NAT.

- [ ] **Step 6: The regression test that matters**

With the stack running, set `S3_ENDPOINT_URL` to a deliberately unresolvable
value, restart the orchestrator, then upload → submit → train → download.

```bash
docker compose exec orchestrator sh -c 'echo $S3_ENDPOINT_URL'
```

Expected: training completes and the model downloads. This passes only if no
peer consults that setting, which is the entire point of the change.

- [ ] **Step 7: Commit**

```bash
git add deploy/ docs/STATUS.md
git commit -m "docs(deploy): reach the fleet from anywhere over one port"
```

---

## Self-Review

**Spec coverage.** Piece 1 → Tasks 2, 3. Piece 2 → Tasks 4, 5, 6. Piece 3 →
Task 7. Piece 4 → Task 8. The spec's "Consequences" section (obsolete
`S3_ENDPOINT_URL` guidance) → Task 8 Steps 1–2. Every spec test in
"Testing" maps to a task: 1→T3, 2→T1/T3, 3→T5, 4→T4, 5→T5, 6→T7, 7→T8 Step 6.

**Type consistency.** `create_dataset_jwt`/`decode_dataset_jwt` and
`create_checkpoint_jwt`/`decode_checkpoint_jwt` are defined in Task 1 and used
under those exact names in Tasks 3, 4 and 6. `public_base_url` is defined in
Task 2 and used in Tasks 3 and 6. `store_from_env` is defined in Task 5 and
consumed by `train.py` in the same task. `put_bytes` on the orchestrator side
is keyword-only (`put_bytes(*, key, data)`, matching `_BucketStore`'s existing
style) while the trainer Protocol's is positional (`put_bytes(key, data)`) —
these are two different classes in two different processes, and each matches
its own module's convention.

**Placeholder scan.** No TBDs. Task 8 is the only task whose deliverables are
prose and config rather than code; its steps name the exact files and the exact
claims each must make, and Step 6 is an executable check.

**Known follow-up, deliberately not in this plan:** `CheckpointObjectStore`
becomes read-write, so its "read-only from the orchestrator's side" docstring
is corrected in Task 4 Step 3. No other consumer of that class exists.
