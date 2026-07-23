"""Training execution telemetry (M4): per-epoch metrics and log transcript.

Both tables are fed exclusively by the WebSocket stream (ADR-002) as frames
arrive from the agent tailing the real trainer container — never backfilled
or reconstructed after the fact. ``TrainingMetric`` is one row per parsed
``{"type":"metric",...}`` line from ``trainer/train.py`` (real measured loss
and held-out test accuracy, never invented). ``TrainingLogLine`` is the raw
stdout/stderr transcript, capped per job so it cannot grow unbounded.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from orchestrator.core.db import Base


class TrainingMetric(Base):
    """One real per-epoch metric line reported by a training run.

    ``test_accuracy`` is nullable in principle (a future metric line might
    report loss without having run an eval pass yet) but ``loss`` is always
    present — every line the trainer emits carries a real measured loss.
    """

    __tablename__ = "training_metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lease_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    # Optional finer-grained step within the epoch; NULL when the trainer only
    # reports one metric line per epoch (train.py's current contract).
    step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    loss: Mapped[float] = mapped_column(Float, nullable=False)
    test_accuracy: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Server receive time — when the orchestrator persisted this frame.
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class TrainingLogLine(Base):
    """One raw stdout/stderr line from the trainer container's real output.

    Retention is capped per job (``orchestrator.services.training``'s
    ``LOG_LINE_RETENTION_PER_JOB``) by trimming the oldest rows on insert — this
    is a transcript tail, not a durable audit log.
    """

    __tablename__ = "training_log_lines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lease_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    # "stdout" | "stderr" — plain string (not a PG enum) since this is an
    # internal transcript detail, not part of any cross-service contract.
    stream: Mapped[str] = mapped_column(String(16), nullable=False)
    line: Mapped[str] = mapped_column(String, nullable=False)
