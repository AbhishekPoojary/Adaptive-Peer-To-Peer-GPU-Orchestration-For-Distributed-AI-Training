"""Job endpoints: submit, list, detail, cancel.

# TODO(M8): user auth. Submit/read/cancel are unauthenticated for dev
# convenience; hardening (M8) must gate them behind real user auth before any
# non-dev deployment.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import get_settings_dep
from orchestrator.core.config import Settings
from orchestrator.core.db import get_session
from orchestrator.schedulers.registry import is_registered, registered_names
from orchestrator.schemas.job import (
    JobDetailResponse,
    JobListResponse,
    JobSubmitRequest,
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

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=JobDetailResponse)
async def submit_job(
    body: JobSubmitRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> JobDetailResponse:
    """Validate the spec, enqueue the job (QUEUED), and trigger a scheduler pass.

    The effective scheduler is the request's ``scheduler_name`` or the server
    default; either way it must be a registered strategy (``adaptive`` is not
    registered until M3, so it is rejected here).

    # TODO(M8): user auth — unauthenticated for dev.
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

    job = await create_job(session, req=body, scheduler_name=scheduler_name)
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
    """List every job, newest first.

    # TODO(M8): user auth — unauthenticated for dev.
    """
    return JobListResponse(jobs=await list_jobs(session))


@router.get("/{job_id}", response_model=JobDetailResponse)
async def get_job_endpoint(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> JobDetailResponse:
    """One job with its full event timeline and leases.

    # TODO(M8): user auth — unauthenticated for dev.
    """
    detail = await get_job_detail(session, job_id=job_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown job")
    return detail


@router.post("/{job_id}/cancel", response_model=JobDetailResponse)
async def cancel_job_endpoint(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> JobDetailResponse:
    """Cancel a non-terminal job, releasing any ACTIVE lease.

    # TODO(M8): user auth — unauthenticated for dev.
    """
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
