"""Scheduling-decision audit models (M3, ADR-009).

Every adaptive placement writes a ``SchedulingDecision`` (the weights in force
and which node won) plus one ``SchedulingDecisionCandidate`` per node that was
considered — with that node's ``L_i``/``R_i``/``D_i``/``S_i`` and the raw inputs
behind them (utilization, RTT, decay-weighted success/failure counts). This is
the "why did this job land here" audit trail ADR-009 requires: persisted, not
just logged to stdout, so the report can reconstruct any decision after the
fact.

Nothing here is fabricated: the numbers are copied verbatim from the real
scoring pass, and a raw input that was genuinely unknown at decision time
(no telemetry / no RTT yet) is stored as SQL ``NULL``, never a placeholder.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from orchestrator.core.db import Base


class SchedulingDecision(Base):
    """One scheduling decision for one job: the weights used and the winner.

    ``selected_node_id`` is NULL only if a decision was recorded with no
    selectable candidate (not written in that case today, but the column stays
    honest about the possibility). The per-candidate breakdown hangs off
    ``candidates``.
    """

    __tablename__ = "scheduling_decisions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    # The strategy that produced this decision (always 'adaptive' in M3, but
    # stored so a future audited strategy is distinguishable).
    scheduler_name: Mapped[str] = mapped_column(String(32), nullable=False)

    # The score weights and reliability parameters in force for this decision,
    # captured so a later config change never rewrites history.
    alpha: Mapped[float] = mapped_column(Float, nullable=False)
    beta: Mapped[float] = mapped_column(Float, nullable=False)
    gamma: Mapped[float] = mapped_column(Float, nullable=False)
    reliability_halflife_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    wilson_z: Mapped[float] = mapped_column(Float, nullable=False)

    # Winner. SET NULL on node delete so the audit row survives a node removal.
    selected_node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("nodes.id", ondelete="SET NULL"),
        nullable=True,
    )

    candidates: Mapped[list[SchedulingDecisionCandidate]] = relationship(
        back_populates="decision",
        cascade="all, delete-orphan",
        order_by="SchedulingDecisionCandidate.s_score",
    )


class SchedulingDecisionCandidate(Base):
    """One node considered in a decision, with its scores and raw inputs.

    ``node_name`` is denormalized so the audit trail stays readable even after a
    node is renamed or deleted (``node_id`` is SET NULL on delete). ``raw_util``
    and ``raw_rtt_ewma_ms`` are NULL when that signal was unknown at decision
    time — the honest representation, per the anti-fabrication rules.
    """

    __tablename__ = "scheduling_decision_candidates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scheduling_decisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("nodes.id", ondelete="SET NULL"),
        nullable=True,
    )
    node_name: Mapped[str] = mapped_column(String(64), nullable=False)

    # Component scores (all in [0, 1] except s_score, which is their weighted sum).
    l_score: Mapped[float] = mapped_column(Float, nullable=False)
    r_score: Mapped[float] = mapped_column(Float, nullable=False)
    d_score: Mapped[float] = mapped_column(Float, nullable=False)
    s_score: Mapped[float] = mapped_column(Float, nullable=False)

    # Raw inputs behind the normalized scores. NULL = genuinely unknown.
    raw_util: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_rtt_ewma_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Decay-weighted pseudo-counts (prior + decayed lease outcomes) feeding R_i.
    weighted_success: Mapped[float] = mapped_column(Float, nullable=False)
    weighted_failure: Mapped[float] = mapped_column(Float, nullable=False)

    was_selected: Mapped[bool] = mapped_column(Boolean, nullable=False)

    decision: Mapped[SchedulingDecision] = relationship(back_populates="candidates")
