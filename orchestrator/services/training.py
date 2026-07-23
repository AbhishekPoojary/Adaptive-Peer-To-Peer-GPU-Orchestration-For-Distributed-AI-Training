"""Training metric/log persistence (M4): fed frame-by-frame by the WebSocket
stream (``orchestrator.api.streaming``) as they arrive — never buffered in
memory and written at the end. Every row here is a real value the trainer (or
the agent tailing it) reported over the wire.

Log-line retention is capped per job (a transcript tail, not a durable audit
log): each insert trims down to the most recent ``LOG_LINE_RETENTION_PER_JOB``
rows for that job. This is intentionally simple (a bounded ORDER BY ... LIMIT
plus a DELETE) rather than a background sweep — fine at this milestone's
single-job-at-a-time scale.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.models.training import TrainingLogLine, TrainingMetric

#: Keep at most this many log lines per job; oldest are trimmed on insert.
LOG_LINE_RETENTION_PER_JOB = 2000


async def record_metric(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    lease_id: uuid.UUID,
    epoch: int,
    loss: float,
    test_accuracy: float | None,
    step: int | None = None,
    ts: datetime | None = None,
) -> TrainingMetric:
    """Persist one real per-epoch metric line. Caller commits."""
    metric = TrainingMetric(
        job_id=job_id,
        lease_id=lease_id,
        epoch=epoch,
        step=step,
        loss=loss,
        test_accuracy=test_accuracy,
        ts=ts or datetime.now(UTC),
    )
    session.add(metric)
    await session.flush()
    return metric


async def record_log_line(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    lease_id: uuid.UUID,
    stream: str,
    line: str,
    ts: datetime | None = None,
) -> TrainingLogLine:
    """Persist one real stdout/stderr line, then trim this job's transcript to
    the retention cap. Caller commits."""
    row = TrainingLogLine(
        job_id=job_id,
        lease_id=lease_id,
        stream=stream,
        line=line,
        ts=ts or datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    await _trim_log_lines(session, job_id=job_id)
    return row


async def _trim_log_lines(session: AsyncSession, *, job_id: uuid.UUID) -> None:
    """Delete this job's log lines beyond the most recent retention cap."""
    keep_ids = (
        (
            await session.execute(
                select(TrainingLogLine.id)
                .where(TrainingLogLine.job_id == job_id)
                .order_by(TrainingLogLine.id.desc())
                .limit(LOG_LINE_RETENTION_PER_JOB)
            )
        )
        .scalars()
        .all()
    )
    if len(keep_ids) < LOG_LINE_RETENTION_PER_JOB:
        return  # under the cap; nothing to trim
    await session.execute(
        delete(TrainingLogLine).where(
            TrainingLogLine.job_id == job_id, TrainingLogLine.id.not_in(keep_ids)
        )
    )


async def list_metrics(session: AsyncSession, *, job_id: uuid.UUID) -> list[TrainingMetric]:
    """This job's real metric rows, oldest first — the loss/accuracy curve."""
    rows = (
        (
            await session.execute(
                select(TrainingMetric)
                .where(TrainingMetric.job_id == job_id)
                .order_by(TrainingMetric.epoch, TrainingMetric.ts)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)
