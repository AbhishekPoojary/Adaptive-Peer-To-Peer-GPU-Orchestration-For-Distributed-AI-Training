"""Read schemas for the scheduling-decision audit trail (M3, ADR-009).

Shapes the ``GET /jobs/{id}/scheduling-decisions`` response: the weights a
decision used and, per node considered, the component scores plus the raw
inputs that justify them. A raw signal that was unknown at decision time is
``null`` here, mirroring how it is stored — never a stand-in number.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class SchedulingCandidateOut(BaseModel):
    """One node considered in a decision, with its scores and raw inputs."""

    node_id: uuid.UUID | None
    node_name: str
    l_score: float
    r_score: float
    d_score: float
    s_score: float
    raw_util: float | None
    raw_rtt_ewma_ms: float | None
    weighted_success: float
    weighted_failure: float
    was_selected: bool


class SchedulingDecisionOut(BaseModel):
    """One scheduling decision: the weights in force and the candidates ranked."""

    id: uuid.UUID
    job_id: uuid.UUID
    ts: datetime
    scheduler_name: str
    alpha: float
    beta: float
    gamma: float
    reliability_halflife_seconds: float
    wilson_z: float
    selected_node_id: uuid.UUID | None
    candidates: list[SchedulingCandidateOut]


class SchedulingDecisionListResponse(BaseModel):
    """Body of GET /jobs/{id}/scheduling-decisions."""

    decisions: list[SchedulingDecisionOut]
