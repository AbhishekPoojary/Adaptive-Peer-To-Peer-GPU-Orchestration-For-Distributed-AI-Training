"""Schemas for the agent-facing lease protocol (ADR-003).

Every mutating call carries ``lease_epoch`` — the fencing token. The server
rejects a write whose epoch is not the job's current epoch (a stale/zombie
holder), so these bodies make the epoch a required, explicit part of the
contract rather than something inferred server-side.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.schemas.job import LeaseOut
from orchestrator.schemas.training import TrainingResultIn

_FORBID = ConfigDict(extra="forbid")


class ClaimResponse(BaseModel):
    """Result of POST /nodes/{id}/leases/claim.

    ``lease`` is the granted lease, or ``null`` when there was no work scheduled
    to this node right now (a clean empty-handed poll, not an error).
    """

    lease: LeaseOut | None


class LeaseEpochRequest(BaseModel):
    """Body carrying just the fencing epoch (renew)."""

    model_config = _FORBID

    lease_epoch: int = Field(ge=1)


class LeaseCompleteRequest(BaseModel):
    """Body of POST /leases/{id}/complete: fencing epoch plus an optional real
    training result summary (M4). ``result`` is omitted entirely by callers
    that have nothing to report (e.g. a non-training job)."""

    model_config = _FORBID

    lease_epoch: int = Field(ge=1)
    result: TrainingResultIn | None = None


class LeaseFailRequest(BaseModel):
    """Body of POST /leases/{id}/fail: fencing epoch, a real reason, and an
    optional real training result summary (M4)."""

    model_config = _FORBID

    lease_epoch: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1024)
    result: TrainingResultIn | None = None


__all__ = [
    "ClaimResponse",
    "LeaseCompleteRequest",
    "LeaseEpochRequest",
    "LeaseFailRequest",
    "LeaseOut",
]
