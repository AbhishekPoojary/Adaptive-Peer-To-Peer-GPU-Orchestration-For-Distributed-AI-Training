"""Node and telemetry-sample models (M1 core).

A ``Node`` is a peer machine that has completed enrollment (ADR-008). Its
``hardware`` inventory and every telemetry sample are recorded exactly as the
agent reported them: absent GPUs are an empty list, and unavailable telemetry
(e.g. no NVML) round-trips as SQL ``NULL`` — never a fabricated number
(CONTRIBUTING.md rules 1-3).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    String,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from orchestrator.core.db import Base


class NodeStatus(enum.Enum):
    """Lifecycle status of a node as observed by the orchestrator."""

    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DRAINING = "DRAINING"


# Named PG enum type. create_type=False: the type is created/dropped explicitly
# by the Alembic migration, not implicitly at column bind time.
_node_status_enum = SAEnum(
    NodeStatus,
    name="node_status",
    create_type=False,
    values_callable=lambda e: [m.value for m in e],
)


class Node(Base):
    """A peer machine enrolled in the fleet."""

    __tablename__ = "nodes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Server-assigned, human-friendly, unique (e.g. "node-01").
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # Agent-generated Ed25519 public key in PEM (SubjectPublicKeyInfo) form.
    public_key: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[NodeStatus] = mapped_column(
        _node_status_enum, nullable=False, default=NodeStatus.OFFLINE
    )
    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Truthful hardware inventory reported at enrollment. Shape:
    #   {hostname, os, cpu_model, cores, ram_bytes,
    #    gpus: [{name, vram_bytes, driver_version}, ...]}  (gpus may be []).
    hardware: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    agent_version: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- Reliability signal (ADR-009). Populated by later milestones from
    # recorded lease outcomes; the schema and declared prior land now. ---
    lease_success_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0", default=0
    )
    lease_failure_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0", default=0
    )
    # Declared Beta(alpha, beta) prior. Defaults to the uniform Beta(1, 1):
    # no reliability is assumed before lease history exists.
    reliability_prior_alpha: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="1.0", default=1.0
    )
    reliability_prior_beta: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="1.0", default=1.0
    )

    # --- Scheduler rotation state (M2). The RoundRobin scheduler derives
    # fairness from this: the eligible node whose last assignment is oldest
    # (NULL = never assigned) is picked next, then this is stamped. NULL for a
    # node the scheduler has never placed a job on. ---
    last_assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    telemetry_samples: Mapped[list[NodeTelemetrySample]] = relationship(
        back_populates="node", cascade="all, delete-orphan"
    )


class NodeTelemetrySample(Base):
    """One point-in-time telemetry reading reported by a node's heartbeat.

    ``gpu`` is NULL when the agent has no GPU telemetry to report (e.g. NVML
    unavailable); it is never substituted with zeros or plausible values.
    ``rtt_ms`` is NULL until the agent has measured at least one round trip.
    """

    __tablename__ = "node_telemetry_samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Server receive time — when the orchestrator recorded this sample.
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    cpu_percent: Mapped[float] = mapped_column(Float, nullable=False)
    ram_used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ram_total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # NULL means "no GPU telemetry available", not "no load". Shape when present:
    #   [{util_percent, mem_used_bytes, mem_total_bytes, temperature_c, power_w}]
    # none_as_null=True: a Python None persists as SQL NULL, never a JSON `null`
    # literal — the absence of telemetry is a true NULL, honestly queryable.
    gpu: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    # Agent-measured RTT from its previous round trip. NULL until first measured.
    rtt_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Server-maintained EWMA over measured rtt_ms. NULL while no RTT measured yet.
    rtt_ewma_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    node: Mapped[Node] = relationship(back_populates="telemetry_samples")
