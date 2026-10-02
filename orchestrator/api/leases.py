"""Agent-facing lease endpoints (ADR-003), all node-authenticated and
self-scoped.

* ``POST /nodes/{node_id}/leases/claim`` — pull-based claim of a job scheduled
  to this node.
* ``POST /leases/{lease_id}/renew|complete|fail`` — epoch-fenced lifecycle
  writes; a stale epoch is rejected 409 and mutates nothing.

Ownership is enforced twice over: ``assert_node_scope`` on the claim path (the
JWT must match the path node), and a lease-ownership check inside the service
for renew/complete/fail (a node may only act on its own lease → 403).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import assert_node_scope, get_settings_dep, require_node_auth
from orchestrator.core.config import Settings
from orchestrator.core.db import get_session
from orchestrator.core.security import (
    JWTValidationError,
    create_checkpoint_jwt,
    decode_checkpoint_jwt,
)
from orchestrator.models.job import Job
from orchestrator.models.lease import Lease
from orchestrator.models.node import Node
from orchestrator.schemas.job import LeaseOut
from orchestrator.schemas.lease import (
    ClaimResponse,
    LeaseCompleteRequest,
    LeaseEpochRequest,
    LeaseFailRequest,
)
from orchestrator.services.checkpoint_access import (
    CheckpointAccessDeniedError,
    authorize,
    authorize_live_lease,
)
from orchestrator.services.datasets import get_dataset
from orchestrator.services.jobs import IllegalTransitionError
from orchestrator.services.leases import (
    LeaseNotActiveError,
    LeaseNotFoundError,
    LeaseScopeError,
    StaleEpochError,
    claim_job_for_node,
    complete_lease,
    fail_lease,
    rendezvous_assignment,
    renew_lease,
)
from orchestrator.services.liveness import record_activity
from orchestrator.services.object_store import (
    CheckpointObjectStore,
    DatasetObjectStore,
    ObjectNotFoundError,
    ObjectStoreError,
)

logger = logging.getLogger("orchestrator.leases")

router = APIRouter(tags=["leases"])


def _lease_out(lease: Lease) -> LeaseOut:
    return LeaseOut(
        id=lease.id,
        job_id=lease.job_id,
        node_id=lease.node_id,
        lease_epoch=lease.lease_epoch,
        rank=lease.rank,
        state=lease.state.value,
        granted_at=lease.granted_at,
        expires_at=lease.expires_at,
        renewed_at=lease.renewed_at,
        released_at=lease.released_at,
    )


_STALE_EPOCH = HTTPException(
    status_code=status.HTTP_409_CONFLICT,
    detail="stale lease epoch; this lease has been superseded (fenced out)",
)
_NOT_ACTIVE = HTTPException(
    status_code=status.HTTP_409_CONFLICT, detail="lease is not active"
)
_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="unknown lease"
)
_NOT_OWNER = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN, detail="lease is not owned by this node"
)


async def _attach_dataset_fetch(
    job_spec: dict[str, Any],
    *,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """Add fetch instructions for an uploaded dataset to a granted spec (ADR-014).

    Mutates ``job_spec`` in place, adding ``dataset_url`` (a presigned GET) and
    ``dataset_sha256``. Built-in datasets are untouched — the trainer downloads
    those from torchvision itself.

    The URL is minted per claim and expires with the lease's useful life, so a
    peer that has finished a job cannot keep reading the data indefinitely, and
    no node ever holds the bucket's credentials. That is the whole reason this
    happens at claim time rather than being baked into the spec at submit time:
    a spec is stored forever, and a signed URL in it would be a long-lived
    credential sitting in the jobs table.

    A dataset that has since been deleted leaves the spec without a URL. The
    agent fails the lease with a clear reason rather than the orchestrator
    refusing the claim, because the job is genuinely unrunnable and should end
    up FAILED with an explanation instead of silently never being granted.
    """
    raw_id = job_spec.get("dataset_id")
    if not raw_id:
        return
    try:
        dataset_id = uuid.UUID(str(raw_id))
    except ValueError:
        logger.warning("job spec carries an unparseable dataset_id %r", raw_id)
        return

    dataset = await get_dataset(session, dataset_id=dataset_id)
    if dataset is None:
        logger.warning(
            "job references dataset %s, which no longer exists; the peer will "
            "fail this lease",
            dataset_id,
        )
        return

    try:
        job_spec["dataset_url"] = DatasetObjectStore(settings).presigned_get_url(
            key=dataset.object_key,
            expires_seconds=settings.dataset_url_ttl_seconds,
        )
    except ObjectStoreError as exc:
        logger.error("could not sign a dataset URL for %s: %s", dataset_id, exc)
        return
    job_spec["dataset_sha256"] = dataset.sha256
    job_spec["dataset_name"] = dataset.name
    job_spec["dataset_num_classes"] = len(dataset.classes)


@router.post("/nodes/{node_id}/leases/claim", response_model=ClaimResponse)
async def claim_lease(
    node_id: uuid.UUID,
    node: Node = Depends(require_node_auth),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> ClaimResponse:
    """Claim this node's assigned rank slot, or return ``lease: null`` if none.

    On a grant the response also carries the rank/world_size/rendezvous endpoint
    (``RendezvousAssignment``) the agent needs to launch the rank under torchrun
    (M5, ADR-005), and the job's spec — which since M8 is the agent's only route
    to it, ``GET /jobs/{id}`` having become human-only (ADR-012)."""
    assert_node_scope(node_id, node)
    lease = await claim_job_for_node(session, node=node, settings=settings)
    if lease is None:
        await session.commit()
        return ClaimResponse(lease=None, rendezvous=None, job_spec=None)

    job = await session.get(Job, lease.job_id)
    assert job is not None  # the lease was just activated against it
    rendezvous = rendezvous_assignment(job, lease, settings=settings)
    job_spec = dict(job.spec)
    await _attach_dataset_fetch(job_spec, session=session, settings=settings)
    checkpoint_token = create_checkpoint_jwt(
        lease_id=str(lease.id),
        job_id=str(job.id),
        signing_key=settings.jwt_signing_key,
        ttl_seconds=settings.checkpoint_token_ttl_seconds,
    )
    await session.commit()
    return ClaimResponse(
        lease=_lease_out(lease),
        rendezvous=rendezvous,
        job_spec=job_spec,
        checkpoint_token=checkpoint_token,
    )


@router.post("/leases/{lease_id}/renew", response_model=LeaseOut)
async def renew(
    lease_id: uuid.UUID,
    body: LeaseEpochRequest,
    node: Node = Depends(require_node_auth),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> LeaseOut:
    """Extend an ACTIVE lease's TTL (epoch-fenced)."""
    record_activity(node.id)
    try:
        lease = await renew_lease(
            session, lease_id=lease_id, node=node, epoch=body.lease_epoch, settings=settings
        )
    except LeaseNotFoundError as exc:
        await session.rollback()
        raise _NOT_FOUND from exc
    except LeaseScopeError as exc:
        await session.rollback()
        raise _NOT_OWNER from exc
    except StaleEpochError as exc:
        await session.rollback()
        raise _STALE_EPOCH from exc
    except LeaseNotActiveError as exc:
        await session.rollback()
        raise _NOT_ACTIVE from exc
    await session.commit()
    return _lease_out(lease)


@router.post("/leases/{lease_id}/complete", response_model=LeaseOut)
async def complete(
    lease_id: uuid.UUID,
    body: LeaseCompleteRequest,
    node: Node = Depends(require_node_auth),
    session: AsyncSession = Depends(get_session),
) -> LeaseOut:
    """Finish a lease successfully (epoch-fenced); job → COMPLETED.

    ``body.result`` is the optional real training result summary (M4); when
    present it is persisted to ``Job.result`` and shapes the plain-language
    completion message.
    """
    try:
        lease = await complete_lease(
            session,
            lease_id=lease_id,
            node=node,
            epoch=body.lease_epoch,
            result=body.result.model_dump() if body.result is not None else None,
        )
    except LeaseNotFoundError as exc:
        await session.rollback()
        raise _NOT_FOUND from exc
    except LeaseScopeError as exc:
        await session.rollback()
        raise _NOT_OWNER from exc
    except StaleEpochError as exc:
        await session.rollback()
        raise _STALE_EPOCH from exc
    except (LeaseNotActiveError, IllegalTransitionError) as exc:
        await session.rollback()
        raise _NOT_ACTIVE from exc
    await session.commit()
    return _lease_out(lease)


@router.post("/leases/{lease_id}/fail", response_model=LeaseOut)
async def fail(
    lease_id: uuid.UUID,
    body: LeaseFailRequest,
    node: Node = Depends(require_node_auth),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> LeaseOut:
    """Finish a lease as failed (epoch-fenced).

    The job is retried on another peer while it has retries left, and fails
    terminally once it does not (ADR-005 addendum 2) — so a trainer killed by
    one machine's OOM killer does not end a job another machine could finish.

    ``body.result`` is the optional real training result summary (M4); when
    present it is persisted to ``Job.result`` and shapes the plain-language
    failure message.
    """
    try:
        lease = await fail_lease(
            session,
            lease_id=lease_id,
            node=node,
            epoch=body.lease_epoch,
            reason=body.reason,
            result=body.result.model_dump() if body.result is not None else None,
            max_failure_retries=settings.max_job_failure_retries,
            failed_attempt_backoff_seconds=settings.failed_attempt_backoff_seconds,
        )
    except LeaseNotFoundError as exc:
        await session.rollback()
        raise _NOT_FOUND from exc
    except LeaseScopeError as exc:
        await session.rollback()
        raise _NOT_OWNER from exc
    except StaleEpochError as exc:
        await session.rollback()
        raise _STALE_EPOCH from exc
    except (LeaseNotActiveError, IllegalTransitionError) as exc:
        await session.rollback()
        raise _NOT_ACTIVE from exc
    await session.commit()
    return _lease_out(lease)


# --- Checkpoint objects (ADR-006 addendum 3) ----------------------------------


def _bearer_token(authorization: str | None, settings: Settings) -> tuple[str, str]:
    """The (lease id, job id) a trainer's lease token names, or 401."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing lease token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return decode_checkpoint_jwt(
            authorization[7:].strip(), signing_key=settings.jwt_signing_key
        )
    except JWTValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid lease token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def _byte_range(header: str | None, size: int | None) -> tuple[int, int] | None:
    """An inclusive (first, last) from ``Range: bytes=a-b`` / ``bytes=a-``.

    Only the single-range forms a downloader sends; anything else, or a range
    that cannot be satisfied, is answered with the whole object (a 200 is
    always a correct reply to a Range request).
    """
    if not header or size is None or not header.startswith("bytes="):
        return None
    first_text, _, last_text = header[6:].partition("-")
    if not first_text.isdigit() or (last_text and not last_text.isdigit()):
        return None
    first = int(first_text)
    last = min(int(last_text), size - 1) if last_text else size - 1
    if first > last:
        return None
    return first, last


@router.get("/leases/{lease_id}/dataset")
async def read_lease_dataset(
    lease_id: uuid.UUID,
    authorization: str | None = Header(default=None),
    range_header: str | None = Header(default=None, alias="Range"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> StreamingResponse:
    """Stream the uploaded dataset of the job this lease is running.

    The trainer used to fetch it straight from MinIO by a presigned URL. That
    URL names this host's LAN address, so a peer joining over the internet --
    through a tunnel, which is how a friend's laptop joins -- could not reach
    it at all, and on the LAN it travelled as plain HTTP. Through here it
    takes the same route and the same encryption as everything else the peer
    does. It is streamed, never buffered: datasets are the one thing here
    that is reliably large.

    Authorised by the lease token, under the same fence as checkpoints: only
    while this lease is the job's live attempt. The trainer still checks the
    archive's SHA-256 before extracting it.
    """
    token_lease, token_job = _bearer_token(authorization, settings)
    try:
        grant = await authorize_live_lease(
            session,
            token_lease_id=token_lease,
            token_job_id=token_job,
            path_lease_id=lease_id,
        )
    except CheckpointAccessDeniedError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    node_id = grant.lease.node_id
    record_activity(node_id)
    raw_id = grant.job.spec.get("dataset_id")
    if not raw_id:
        raise HTTPException(status_code=404, detail="this job trains on a built-in dataset")
    dataset = await get_dataset(session, dataset_id=uuid.UUID(str(raw_id)))
    # Read before the rollback: it expires loaded rows, and touching one after
    # would attempt a lazy load outside the async context.
    object_key = dataset.object_key if dataset is not None else None
    # Release the connection before a download that may run for minutes.
    await session.rollback()
    if object_key is None:
        raise HTTPException(status_code=404, detail="the job's dataset has been deleted")

    store = DatasetObjectStore(settings)
    size = await asyncio.to_thread(store.head_size_bytes, key=object_key)
    # Byte ranges let the trainer download a large archive as many short
    # requests: a tunnel cuts any single request after a minute or two (the
    # same limit that made uploads chunked), and a failed piece is retried
    # alone instead of restarting a multi-gigabyte download.
    span = _byte_range(range_header, size)
    headers: dict[str, str] = {"Accept-Ranges": "bytes"}
    if span is not None:
        headers["Content-Length"] = str(span[1] - span[0] + 1)
        headers["Content-Range"] = f"bytes {span[0]}-{span[1]}/{size}"
    elif size is not None:
        headers["Content-Length"] = str(size)
    try:
        chunks = store.iter_object(key=object_key, byte_range=span)
        first = await asyncio.to_thread(next, chunks, b"")
    except ObjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="the dataset archive is missing") from exc
    except ObjectStoreError as exc:
        raise HTTPException(status_code=503, detail="dataset storage is unreachable") from exc

    def body():  # type: ignore[no-untyped-def]
        # Each chunk is pulled only as the peer consumes the last one, so this
        # is live evidence the node is there, for the whole of a long piece.
        yield first
        for chunk in chunks:
            record_activity(node_id)
            yield chunk

    return StreamingResponse(
        body(),
        status_code=206 if span is not None else 200,
        media_type="application/zip",
        headers=headers,
    )


async def _checkpoint_grant(
    *,
    lease_id: uuid.UUID,
    key: str,
    write: bool,
    authorization: str | None,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """Authenticate the trainer's lease token and apply the checkpoint rules."""
    token_lease, token_job = _bearer_token(authorization, settings)
    try:
        grant = await authorize(
            session,
            token_lease_id=token_lease,
            token_job_id=token_job,
            path_lease_id=lease_id,
            key=key,
            write=write,
        )
    except CheckpointAccessDeniedError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    record_activity(grant.lease.node_id)


@router.get("/leases/{lease_id}/checkpoint-objects/{key:path}")
async def read_checkpoint_object(
    lease_id: uuid.UUID,
    key: str,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    """A trainer reads its job's manifest or a checkpoint blob.

    404 when the object does not exist, which on the manifest is the ordinary
    answer for a first attempt ("nothing to resume from"). 503 when storage is
    unreachable, kept distinct so the trainer's log says which of the two
    happened rather than reporting an outage as "no prior checkpoint".
    """
    await _checkpoint_grant(
        lease_id=lease_id,
        key=key,
        write=False,
        authorization=authorization,
        session=session,
        settings=settings,
    )
    await session.rollback()  # read-only; release the connection before I/O
    store = CheckpointObjectStore(settings)
    try:
        data = await asyncio.to_thread(store.get_bytes, key=key)
    except ObjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="no such checkpoint object") from exc
    except ObjectStoreError as exc:
        logger.warning("checkpoint read of %s failed: %s", key, exc)
        raise HTTPException(status_code=503, detail="checkpoint storage is unreachable") from exc
    return Response(content=data, media_type="application/octet-stream")


@router.put(
    "/leases/{lease_id}/checkpoint-objects/{key:path}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def write_checkpoint_object(
    lease_id: uuid.UUID,
    key: str,
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    """Rank 0 of the live attempt stores a checkpoint blob or its manifest.

    The size is checked against the declared length before the body is read
    and again after, so an oversized upload is refused without buffering it
    when the client is honest about its length and still refused when not.
    """
    await _checkpoint_grant(
        lease_id=lease_id,
        key=key,
        write=True,
        authorization=authorization,
        session=session,
        settings=settings,
    )
    await session.rollback()
    too_large = HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail=f"checkpoint objects are limited to {settings.checkpoint_max_bytes} bytes",
    )
    declared = request.headers.get("content-length")
    limit = settings.checkpoint_max_bytes
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise too_large
    data = await request.body()
    if len(data) > limit:
        raise too_large

    store = CheckpointObjectStore(settings)
    try:
        await asyncio.to_thread(store.ensure_bucket)
        await asyncio.to_thread(store.put_bytes, key=key, data=data)
    except ObjectStoreError as exc:
        logger.warning("checkpoint write of %s failed: %s", key, exc)
        raise HTTPException(status_code=503, detail="checkpoint storage is unreachable") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
