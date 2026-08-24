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

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import assert_node_scope, get_settings_dep, require_node_auth
from orchestrator.core.config import Settings
from orchestrator.core.db import get_session
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
from orchestrator.services.object_store import DatasetObjectStore, ObjectStoreError

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
    await session.commit()
    return ClaimResponse(
        lease=_lease_out(lease), rendezvous=rendezvous, job_spec=job_spec
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
