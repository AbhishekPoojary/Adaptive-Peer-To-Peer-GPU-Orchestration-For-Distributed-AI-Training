"""Job endpoints: submit, list, detail, cancel.

Every route here is human-facing and requires an authenticated user (ADR-012).
The dependency is declared on the *router*, not per-route, so a route added
later is gated by default — the failure mode of the per-route form is a new
endpoint silently shipping open, and that is exactly how this surface came to
be unauthenticated in the first place.

Node-facing lease reporting lives in ``api/leases.py`` under node auth; a node
token is rejected here (wrong JWT audience) and vice versa.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import get_settings_dep, require_user
from orchestrator.core.config import Settings
from orchestrator.core.db import get_session
from orchestrator.models.user import User
from orchestrator.schedulers.registry import is_registered, registered_names
from orchestrator.schemas.job import (
    CheckpointOut,
    JobDetailResponse,
    JobListResponse,
    JobSubmitRequest,
)
from orchestrator.schemas.scheduling import SchedulingDecisionListResponse
from orchestrator.schemas.training import (
    TrainingLogLineListResponse,
    TrainingLogLineOut,
    TrainingMetricListResponse,
    TrainingMetricOut,
)
from orchestrator.services.checkpoints import (
    latest_checkpoint_for,
    stream_checkpoint,
    suggested_filename,
)
from orchestrator.services.datasets import get_dataset
from orchestrator.services.jobs import (
    IllegalTransitionError,
    JobNotFoundError,
    cancel_job,
    create_job,
    get_job_detail,
    list_jobs,
)
from orchestrator.services.loops import trigger_scheduler_pass
from orchestrator.services.object_store import ObjectStoreError
from orchestrator.services.scheduling import list_scheduling_decisions
from orchestrator.services.training import (
    LOG_LINES_DEFAULT_LIMIT,
    LOG_LINES_MAX_LIMIT,
    list_log_lines,
    list_metrics,
)

logger = logging.getLogger("orchestrator.jobs")

router = APIRouter(
    prefix="/jobs", tags=["jobs"], dependencies=[Depends(require_user)]
)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=JobDetailResponse)
async def submit_job(
    body: JobSubmitRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> JobDetailResponse:
    """Validate the spec, enqueue the job (QUEUED), and trigger a scheduler pass.

    The effective scheduler is the request's ``scheduler_name`` or the server
    default; either way it must be a registered strategy (``adaptive`` is not
    registered until M3, so it is rejected here).

    Attribution comes from the authenticated token, not the request body
    (ADR-012 §4). A caller cannot claim to be someone else, so the dashboard's
    "submitted by" column is now evidence rather than decoration.
    """
    scheduler_name = body.scheduler_name or settings.scheduler_strategy
    if not is_registered(scheduler_name):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"unknown scheduler '{scheduler_name}'; "
                f"registered: {sorted(registered_names())}"
            ),
        )

    # Resolve an uploaded dataset now (ADR-014). Checking at submit time means a
    # bad reference is a 422 the submitter sees immediately, rather than a job
    # that queues, waits for a peer, and dies twenty minutes later on a machine
    # nobody is watching.
    if body.spec.dataset_id is not None:
        dataset = await get_dataset(session, dataset_id=body.spec.dataset_id)
        if dataset is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "unknown or deleted dataset; pick one from GET /datasets"
                ),
            )

    job = await create_job(
        session,
        req=body,
        scheduler_name=scheduler_name,
        submitted_by=user.username,
    )
    await session.commit()

    # Place it now rather than waiting for the periodic loop. Its own session/txn.
    await trigger_scheduler_pass(settings)

    # The pass committed from a separate session; get_job_detail reads with
    # populate_existing so this reflects committed DB truth, not a cached copy.
    detail = await get_job_detail(session, job_id=job.id)
    assert detail is not None  # just created and committed
    return detail


@router.get("", response_model=JobListResponse)
async def list_jobs_endpoint(
    session: AsyncSession = Depends(get_session),
) -> JobListResponse:
    """List every job, newest first."""
    return JobListResponse(jobs=await list_jobs(session))


@router.get("/{job_id}", response_model=JobDetailResponse)
async def get_job_endpoint(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> JobDetailResponse:
    """One job with its full event timeline and leases."""
    detail = await get_job_detail(session, job_id=job_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown job")
    return detail


@router.get(
    "/{job_id}/scheduling-decisions",
    response_model=SchedulingDecisionListResponse,
)
async def get_job_scheduling_decisions(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> SchedulingDecisionListResponse:
    """The adaptive scheduler's audit trail for this job (ADR-009).

    One decision per scheduling pass that considered the job, newest first, each
    with the weights used and the per-candidate L/R/D/S breakdown that explains
    the pick. Empty for a job placed by a baseline scheduler (only ``adaptive``
    records decisions) or never scheduled. 404 only if the job itself is unknown.
    """
    if await get_job_detail(session, job_id=job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown job")
    decisions = await list_scheduling_decisions(session, job_id=job_id)
    return SchedulingDecisionListResponse(decisions=decisions)


@router.get("/{job_id}/checkpoint", response_model=CheckpointOut)
async def get_job_checkpoint(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> CheckpointOut:
    """The trained model this job produced, and a short-lived link to download it.

    Until this existed the system produced a model with no front door: the blob
    sat in MinIO and the only way to it was the storage console with separate
    credentials. A job page that shows 99% accuracy and cannot hand you the
    thing that achieved it is a demo of training, not a tool.

    Any authenticated user may fetch it, matching the rest of this router — a
    person who can read a job's loss curve is not meaningfully restrained by
    being denied its weights.

    404 means the job never checkpointed. That is ordinary: checkpointing needs
    S3 configured on the peer (ADR-006), so a fleet running without it trains
    perfectly well and saves nothing.
    """
    if await get_job_detail(session, job_id=job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown job")

    try:
        found = latest_checkpoint_for(str(job_id), settings=settings)
    except ObjectStoreError as exc:
        # Storage being unreachable is not "no checkpoint". Reporting it as 404
        # would send someone hunting for a training bug that does not exist.
        logger.warning("could not read the checkpoint manifest for %s: %s", job_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "checkpoint storage is unreachable, so whether this job saved a "
                "model cannot be determined right now"
            ),
        ) from exc

    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "this job has no saved model. Checkpointing requires object "
                "storage configured on the peer that ran it (ADR-006)"
            ),
        )

    return CheckpointOut(
        key=found.entry.key,
        step=found.entry.step,
        epoch=found.entry.epoch,
        loss=found.entry.loss,
        world_size=found.entry.world_size,
        timestamp_utc=found.entry.timestamp_utc,
        size_bytes=found.size_bytes,
        download_path=f"/jobs/{job_id}/checkpoint/download",
        filename=suggested_filename(str(job_id), found.entry),
    )


@router.get("/{job_id}/checkpoint/download")
async def download_job_checkpoint(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> StreamingResponse:
    """Stream the trained model's bytes.

    The key is resolved from the manifest here rather than taken from the
    caller, so this cannot be turned into a read of any object in the
    checkpoints bucket by passing a crafted key — the only thing a caller
    chooses is which job.

    The body is streamed in chunks, so a large model is not held in the
    orchestrator's memory. boto3 is blocking, but Starlette iterates a sync
    generator in a threadpool, so the event loop keeps serving.
    """
    if await get_job_detail(session, job_id=job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown job")

    try:
        found = latest_checkpoint_for(str(job_id), settings=settings)
    except ObjectStoreError as exc:
        logger.warning("could not read the checkpoint manifest for %s: %s", job_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="checkpoint storage is unreachable",
        ) from exc

    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="this job has no saved model",
        )

    filename = suggested_filename(str(job_id), found.entry)
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if found.size_bytes is not None:
        # Only when actually known — a wrong Content-Length truncates the
        # download, and a guessed one would be worse than none.
        headers["Content-Length"] = str(found.size_bytes)

    return StreamingResponse(
        stream_checkpoint(found.entry.key, settings=settings),
        media_type="application/octet-stream",
        headers=headers,
    )


@router.get("/{job_id}/metrics", response_model=TrainingMetricListResponse)
async def get_job_metrics(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> TrainingMetricListResponse:
    """This job's real per-epoch metrics (M4), oldest first — the loss curve.

    Fed exclusively by the WebSocket log/metric stream as a real training run
    reports them; empty for a job that hasn't trained yet. 404 only if the job
    itself is unknown.
    """
    if await get_job_detail(session, job_id=job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown job")
    rows = await list_metrics(session, job_id=job_id)
    return TrainingMetricListResponse(
        metrics=[
            TrainingMetricOut(
                id=m.id,
                job_id=m.job_id,
                lease_id=m.lease_id,
                epoch=m.epoch,
                step=m.step,
                loss=m.loss,
                test_accuracy=m.test_accuracy,
                ts=m.ts,
            )
            for m in rows
        ]
    )


@router.get("/{job_id}/logs", response_model=TrainingLogLineListResponse)
async def get_job_logs(
    job_id: uuid.UUID,
    after: int | None = Query(
        default=None,
        ge=0,
        description="Return only lines with id > after (cursor). Omit to start "
        "from the beginning of the retained transcript.",
    ),
    limit: int = Query(default=LOG_LINES_DEFAULT_LIMIT, ge=1, le=LOG_LINES_MAX_LIMIT),
    session: AsyncSession = Depends(get_session),
) -> TrainingLogLineListResponse:
    """This job's real stdout/stderr transcript, cursor-paginated.

    Fed exclusively by the WebSocket log stream (ADR-002) as the trainer
    container actually produces output; empty for a job that hasn't started
    executing. Poll with ``after=<last id you saw>`` to fetch only new lines.
    404 only if the job itself is unknown.
    """
    if await get_job_detail(session, job_id=job_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown job")
    rows = await list_log_lines(session, job_id=job_id, after=after, limit=limit)
    next_after = rows[-1].id if rows else after
    return TrainingLogLineListResponse(
        lines=[
            TrainingLogLineOut(id=r.id, ts=r.ts, stream=r.stream, line=r.line)  # type: ignore[arg-type]
            for r in rows
        ],
        next_after=next_after,
    )


@router.post("/{job_id}/cancel", response_model=JobDetailResponse)
async def cancel_job_endpoint(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> JobDetailResponse:
    """Cancel a non-terminal job, releasing any ACTIVE lease."""
    try:
        await cancel_job(session, job_id=job_id)
    except JobNotFoundError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown job"
        ) from exc
    except IllegalTransitionError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"job is already terminal ({exc.from_state.value}); cannot cancel",
        ) from exc
    await session.commit()

    detail = await get_job_detail(session, job_id=job_id)
    assert detail is not None
    return detail
