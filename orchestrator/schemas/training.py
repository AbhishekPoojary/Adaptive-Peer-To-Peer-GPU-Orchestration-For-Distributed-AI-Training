"""Schemas for training execution results and the metric read API (M4).

``TrainingResultIn`` validates the optional result summary a leaseholder may
attach to ``/leases/{id}/complete`` or ``/leases/{id}/fail`` — every field
mirrors a real value the trainer reported (or is left ``null`` when the
trainer never got far enough to report it); nothing here is a plausible
placeholder. ``TrainingMetricOut`` is the read-side view of one real
per-epoch metric row fed by the WebSocket stream (ADR-002).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_FORBID = ConfigDict(extra="forbid")


class TrainingResultIn(BaseModel):
    """Optional result summary attached to a lease's terminal REST call.

    All fields are individually nullable: a Docker launch failure before the
    trainer ever started means every field but ``exit_code`` (itself possibly
    unknown) is genuinely unknown, and ``null`` is the honest representation
    — never a fabricated 0 or "cpu" guess.
    """

    model_config = _FORBID

    final_loss: float | None = Field(default=None, ge=0)
    final_test_accuracy: float | None = Field(default=None, ge=0, le=1)
    epochs_completed: int = Field(default=0, ge=0)
    exit_code: int | None = None
    device: Literal["cuda", "cpu"] | None = None


class TrainingMetricOut(BaseModel):
    """One real per-epoch metric row, as recorded from the WebSocket stream."""

    id: int
    job_id: uuid.UUID
    lease_id: uuid.UUID
    epoch: int
    step: int | None
    loss: float
    test_accuracy: float | None
    ts: datetime


class TrainingMetricListResponse(BaseModel):
    """Body of GET /jobs/{job_id}/metrics — this job's real loss/accuracy curve."""

    metrics: list[TrainingMetricOut]


class TrainingLogLineOut(BaseModel):
    """One real stdout/stderr line from the trainer container's transcript."""

    id: int
    ts: datetime
    stream: Literal["stdout", "stderr"]
    line: str


class TrainingLogLineListResponse(BaseModel):
    """Body of GET /jobs/{job_id}/logs — a page of this job's real log transcript.

    ``next_after`` is the cursor to pass as ``after`` on the next poll so only
    newly-arrived lines are fetched; it is the id of the last line in this page,
    or the request's own ``after`` (unchanged) when the page was empty — never
    fabricated when there is nothing new yet.
    """

    lines: list[TrainingLogLineOut]
    next_after: int | None


__all__ = [
    "TrainingLogLineListResponse",
    "TrainingLogLineOut",
    "TrainingMetricListResponse",
    "TrainingMetricOut",
    "TrainingResultIn",
]
