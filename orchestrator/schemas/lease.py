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


class RendezvousAssignment(BaseModel):
    """How a claimed rank joins its training cohort (ADR-005, M5).

    Everything an agent needs to launch its rank under ``torchrun`` with real
    c10d rendezvous. ``world_size == 1`` marks the single-process path: the
    agent runs the trainer directly (no torchrun), preserving M4 behaviour, and
    the rendezvous fields are unused. For ``world_size > 1`` every rank dials the
    same ``endpoint`` (``--rdzv-endpoint``) with the same ``rdzv_id``
    (``--rdzv-id``); the rendezvous host (rank 0, ``is_rendezvous_host``) is the
    one whose container is reachable at that endpoint.
    """

    rank: int
    world_size: int
    is_rendezvous_host: bool
    # torch.distributed backend (gloo for M5; nccl once real multi-GPU exists).
    backend: str
    # host:port every rank dials for c10d rendezvous (--rdzv-endpoint).
    endpoint: str
    # Shared rendezvous identifier for this attempt (--rdzv-id).
    rdzv_id: str
    # Dev co-located wiring: the shared user-defined Docker network the cohort's
    # containers join, and the network-alias/name the rank-0 (rendezvous host)
    # container takes so ``endpoint`` resolves to it. Empty for world_size==1.
    network: str
    host_alias: str
    # torchrun --max-restarts (bounded elastic re-formation, ADR-005).
    max_restarts: int


class ClaimResponse(BaseModel):
    """Result of POST /nodes/{id}/leases/claim.

    ``lease`` is the granted lease, or ``null`` when there was no work scheduled
    to this node right now (a clean empty-handed poll, not an error).
    ``rendezvous`` accompanies a granted lease with the rank/world_size/endpoint
    the agent needs to launch it (M5); ``null`` when ``lease`` is ``null``.
    """

    lease: LeaseOut | None
    rendezvous: RendezvousAssignment | None = None


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
    "RendezvousAssignment",
]
