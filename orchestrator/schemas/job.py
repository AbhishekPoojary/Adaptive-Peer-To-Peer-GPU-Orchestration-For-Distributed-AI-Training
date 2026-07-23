"""Schemas for job submission, spec validation, and read views.

The spec is validated as a security/data-integrity boundary: unknown fields are
rejected, and every numeric bound mirrors reality (positive epochs, batch size,
learning rate; ``world_size >= 1``). ``min_gpu_mem_bytes`` is nullable and its
meaning is load-bearing — ``null`` means "CPU-eligible", a distinct requirement
from any numeric value, and is never coerced to 0.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

_FORBID = ConfigDict(extra="forbid")


class JobSpec(BaseModel):
    """A training job's specification."""

    model_config = _FORBID

    dataset: Literal["cifar10", "mnist"]
    model: str = Field(min_length=1, max_length=128)
    epochs: int = Field(ge=1)
    batch_size: int = Field(ge=1)
    learning_rate: float = Field(gt=0)
    world_size: int = Field(ge=1)
    # NULL means the job is CPU-eligible (no GPU-memory requirement). A value is
    # a hard minimum a candidate node's largest GPU must meet.
    min_gpu_mem_bytes: int | None = Field(default=None, ge=0)


class JobSubmitRequest(BaseModel):
    """Body of POST /jobs.

    ``scheduler_name`` is optional; when omitted the server's configured
    ``SCHEDULER_STRATEGY`` default is used. Whatever is chosen must be a
    registered scheduler (validated in the handler).
    """

    model_config = _FORBID

    spec: JobSpec
    scheduler_name: str | None = Field(default=None, max_length=32)
    submitted_by: str = Field(min_length=1, max_length=255)


class JobEventOut(BaseModel):
    """One recorded state transition for the job timeline."""

    ts: datetime
    from_state: str | None
    to_state: str
    detail: dict[str, Any]


class LeaseOut(BaseModel):
    """A lease row as returned to clients."""

    id: uuid.UUID
    job_id: uuid.UUID
    node_id: uuid.UUID
    lease_epoch: int
    state: str
    granted_at: datetime
    expires_at: datetime
    renewed_at: datetime | None
    released_at: datetime | None


class JobSummary(BaseModel):
    """A job's core fields for the list view."""

    id: uuid.UUID
    spec: dict[str, Any]
    scheduler_name: str
    state: str
    current_lease_epoch: int
    scheduled_node_id: uuid.UUID | None
    submitted_by: str
    submitted_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    failure_reason: str | None
    # Real training result summary (M4), or null until a leaseholder reports
    # one: {"final_loss", "final_test_accuracy", "epochs_completed",
    # "exit_code", "device"}. Returned as a free-form dict like `spec` — the
    # write boundary (TrainingResultIn) is what validates it.
    result: dict[str, Any] | None = None


class JobListResponse(BaseModel):
    """Body of GET /jobs."""

    jobs: list[JobSummary]


class JobDetailResponse(JobSummary):
    """Body of GET /jobs/{id}: the summary plus its full audit trail and leases."""

    events: list[JobEventOut]
    leases: list[LeaseOut]
