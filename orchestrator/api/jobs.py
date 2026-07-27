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

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import get_settings_dep, require_user
from orchestrator.core.config import Settings
from orchestrator.core.db import get_session
from orchestrator.models.user import User
from orchestrator.schedulers.registry import is_registered, registered_names
from orchestrator.schemas.job import (
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
from orchestrator.services.jobs import (
    IllegalTransitionError,
    JobNotFoundError,
    cancel_job,
    create_job,
    get_job_detail,
    list_jobs,
)
from orchestrator.services.loops import trigger_scheduler_pass
from orchestrator.services.scheduling import list_scheduling_decisions
from orchestrator.services.training import (
    LOG_LINES_DEFAULT_LIMIT,
    LOG_LINES_MAX_LIMIT,
    list_log_lines,
    list_metrics,
)

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
