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

from pydantic import BaseModel, ConfigDict, Field, model_validator

_FORBID = ConfigDict(extra="forbid")


#: The datasets the trainer can fetch for itself, by name. Anything else must be
#: uploaded first and referenced by id (ADR-014).
BuiltinDataset = Literal["cifar10", "mnist"]


class JobSpec(BaseModel):
    """A training job's specification.

    Exactly one dataset source must be given: ``dataset`` for a built-in that the
    trainer downloads from torchvision, or ``dataset_id`` for an uploaded one
    (ADR-014). Keeping the built-in field exactly as it was means every existing
    caller — the bench harness, the dashboard, 89 historical jobs — still
    validates unchanged, which matters because this model forbids extra fields
    and would otherwise 422 all of them.
    """

    model_config = _FORBID

    dataset: BuiltinDataset | None = None
    #: An uploaded dataset's id. Resolved at submit time (so a bad reference
    #: fails immediately, not on some peer twenty minutes later) and again at
    #: claim time, when the peer is told where to fetch it.
    dataset_id: uuid.UUID | None = None
    model: str = Field(min_length=1, max_length=128)
    epochs: int = Field(ge=1)
    batch_size: int = Field(ge=1)
    learning_rate: float = Field(gt=0)
    world_size: int = Field(ge=1)
    # NULL means the job is CPU-eligible (no GPU-memory requirement). A value is
    # a hard minimum a candidate node's largest GPU must meet.
    min_gpu_mem_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _exactly_one_dataset_source(self) -> JobSpec:
        """Require exactly one of ``dataset`` / ``dataset_id``.

        Neither is not a sensible default — there is nothing to train on — and
        both is ambiguous in a way that would be resolved silently and wrongly
        by whichever branch the trainer happened to check first.
        """
        if self.dataset is None and self.dataset_id is None:
            raise ValueError(
                "a job needs a dataset: pass 'dataset' for a built-in "
                "(cifar10, mnist) or 'dataset_id' for an uploaded one"
            )
        if self.dataset is not None and self.dataset_id is not None:
            raise ValueError(
                "pass either 'dataset' or 'dataset_id', not both"
            )
        return self


class JobSubmitRequest(BaseModel):
    """Body of POST /jobs.

    ``scheduler_name`` is optional; when omitted the server's configured
    ``SCHEDULER_STRATEGY`` default is used. Whatever is chosen must be a
    registered scheduler (validated in the handler).

    There is deliberately no ``submitted_by`` field. Attribution is taken from
    the authenticated user's token (ADR-012 §4); because this model forbids
    extra fields, a client still sending the old field gets a loud 422 rather
    than silently having its claimed identity ignored.
    """

    model_config = _FORBID

    spec: JobSpec
    scheduler_name: str | None = Field(default=None, max_length=32)


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
    # Rank slot 0..world_size-1 within the job's attempt (M5). 0 for single-rank.
    rank: int
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
    # How many attempts ended in a reported trainer failure (ADR-005 addendum
    # 2). Exposed so "this job has been retried twice" is visible rather than
    # something an operator has to infer from the event timeline.
    failed_attempt_count: int
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


class CheckpointOut(BaseModel):
    """This job's latest saved model, and where to get it (ADR-006 addendum 2).

    The values come from the manifest the trainer wrote in object storage, which
    ADR-006 makes the authority on which checkpoint is good — the blob-then-
    manifest write order means a manifest entry only exists once its blob is
    fully stored. Nothing here is inferred from the database, which knows a
    checkpoint key only in the narrower case where a job resumed from one.
    """

    #: Object key of the checkpoint blob. Shown so someone with MinIO access can
    #: find it directly, and so a downloaded file can be traced back.
    key: str
    #: Training step and the epoch the run would resume into.
    step: int
    epoch: int
    #: Loss recorded when this checkpoint was written, or null if the manifest
    #: entry carries none. Never substituted with a plausible number.
    loss: float | None
    world_size: int
    timestamp_utc: str
    #: Size in bytes, or null when object storage would not report it.
    size_bytes: int | None
    #: Where to GET the bytes, relative to the API root. The orchestrator
    #: streams them rather than handing out a presigned storage URL: it signs
    #: against its own S3 endpoint, which in the compose topology is
    #: ``http://minio:9000`` — correct for a peer on that network and
    #: unresolvable from a browser on the host. A presigned URL is signed for one
    #: specific host, so it cannot be rewritten client-side without breaking the
    #: signature. Streaming works from anywhere the API is reachable, including
    #: through the dashboard's dev proxy and over the ADR-010 overlay.
    download_path: str
    #: Suggested filename, so a downloaded file is identifiable a week later
    #: rather than being another "archive.pt" in the downloads folder.
    filename: str
    #: What the file is, in one line, so nobody has to guess what to do with it.
    format: str = (
        "PyTorch checkpoint (torch.save) containing model and optimizer state"
    )
