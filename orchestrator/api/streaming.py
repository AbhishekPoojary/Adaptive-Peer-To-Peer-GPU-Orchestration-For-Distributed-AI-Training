"""Agent -> orchestrator log/metric WebSocket stream (ADR-002, M4).

``WS /nodes/{node_id}/leases/{lease_id}/stream?epoch={lease_epoch}`` is the
transport an agent dials out on (never the reverse — ADR-002) to forward the
real trainer container's stdout/stderr and parsed metric lines as they
happen. Each frame is persisted to ``TrainingLogLine``/``TrainingMetric`` the
moment it arrives — never buffered in memory and flushed at the end.

Auth mirrors the REST lease endpoints: the same node bearer JWT, read from the
``Authorization`` header at the WebSocket handshake. Authorization has three
checks, in order: token validity, node-path scope match, and epoch fencing —
the query's ``epoch`` must equal the lease's job's *current* lease epoch, so a
zombie agent from a reassigned job (stale epoch) is rejected exactly like the
REST epoch fence in ``orchestrator.services.leases``. The decision logic lives
in :func:`authorize_stream`, factored out of the WebSocket transport handling
specifically so it can be unit-tested directly against a real Postgres-backed
session without needing an actual WebSocket connection (see
``tests/test_streaming_auth.py``).

Because a WebSocket handshake cannot carry an HTTP status body the way a REST
401/403/409 can, rejection is expressed as an accept-then-immediate-close with
a distinct application close code (4401/4403/4404/4409) — chosen from the
4000-4999 range RFC 6455 reserves for private use.
"""

from __future__ import annotations

import enum
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import get_settings_dep
from orchestrator.core.config import Settings
from orchestrator.core.db import get_session
from orchestrator.core.security import JWTValidationError, decode_node_jwt
from orchestrator.models.job import Job
from orchestrator.models.lease import Lease, LeaseState
from orchestrator.services.training import record_log_line, record_metric

logger = logging.getLogger("orchestrator.streaming")

router = APIRouter(tags=["streaming"])

# Application-specific WebSocket close codes (RFC 6455 private-use range).
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404
CLOSE_STALE_EPOCH = 4409

_BEARER_PREFIX = "Bearer "


class StreamAuthOutcome(enum.Enum):
    """The result of authorizing one stream connection attempt."""

    OK = "OK"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    STALE_EPOCH = "STALE_EPOCH"


#: Maps each rejection outcome to its WebSocket close code.
OUTCOME_CLOSE_CODES: dict[StreamAuthOutcome, int] = {
    StreamAuthOutcome.UNAUTHORIZED: CLOSE_UNAUTHORIZED,
    StreamAuthOutcome.FORBIDDEN: CLOSE_FORBIDDEN,
    StreamAuthOutcome.NOT_FOUND: CLOSE_NOT_FOUND,
    StreamAuthOutcome.STALE_EPOCH: CLOSE_STALE_EPOCH,
}


@dataclass
class StreamAuthResult:
    """The decision, plus the job id (only present on :attr:`StreamAuthOutcome.OK`)
    and a human-readable reason (only meaningful on rejection)."""

    outcome: StreamAuthOutcome
    job_id: uuid.UUID | None = None
    reason: str = ""


async def authorize_stream(
    session: AsyncSession,
    *,
    node_id: uuid.UUID,
    lease_id: uuid.UUID,
    epoch: int,
    authorization_header: str | None,
    settings: Settings,
) -> StreamAuthResult:
    """Decide whether this stream connection attempt may proceed.

    Checks, in order: bearer token validity, node-path scope match, lease
    existence/ownership, and epoch fencing (the query's ``epoch`` must equal
    the lease's job's *current* lease epoch and the lease must still be
    ACTIVE — a stale/superseded epoch, e.g. a zombie agent from a reassigned
    job, is rejected exactly like the REST epoch fence in
    ``orchestrator.services.leases``).
    """
    if authorization_header is None or not authorization_header.startswith(_BEARER_PREFIX):
        return StreamAuthResult(StreamAuthOutcome.UNAUTHORIZED, reason="missing bearer token")
    token = authorization_header[len(_BEARER_PREFIX) :].strip()
    try:
        subject = decode_node_jwt(token, signing_key=settings.jwt_signing_key)
        token_node_id = uuid.UUID(subject)
    except (JWTValidationError, ValueError):
        return StreamAuthResult(
            StreamAuthOutcome.UNAUTHORIZED, reason="invalid or expired token"
        )
    if token_node_id != node_id:
        return StreamAuthResult(
            StreamAuthOutcome.FORBIDDEN, reason="token is not scoped to this node"
        )

    lease = await session.get(Lease, lease_id)
    if lease is None or lease.node_id != node_id:
        return StreamAuthResult(StreamAuthOutcome.NOT_FOUND, reason="unknown lease")

    job = await session.get(Job, lease.job_id)
    if (
        job is None
        or lease.state is not LeaseState.ACTIVE
        or job.current_lease_epoch != epoch
        or lease.lease_epoch != epoch
    ):
        return StreamAuthResult(StreamAuthOutcome.STALE_EPOCH, reason="stale lease epoch")

    return StreamAuthResult(StreamAuthOutcome.OK, job_id=job.id)


def _frame_ts(raw: object) -> datetime:
    """Parse the frame's agent-side ``ts`` (unix epoch seconds) if present and
    valid; fall back to the server's receive time otherwise. Never raises."""
    if isinstance(raw, int | float):
        try:
            return datetime.fromtimestamp(float(raw), tz=UTC)
        except (OverflowError, OSError, ValueError):
            pass
    return datetime.now(UTC)


async def _persist_frame(
    session: AsyncSession, *, job_id: uuid.UUID, lease_id: uuid.UUID, frame: dict[str, Any]
) -> None:
    """Persist one validated frame; malformed frames are logged and dropped —
    never crash the stream over one bad line from a misbehaving agent build."""
    frame_type = frame.get("type")
    ts = _frame_ts(frame.get("ts"))

    if frame_type == "log":
        stream = frame.get("stream")
        line = frame.get("line")
        if stream not in ("stdout", "stderr") or not isinstance(line, str):
            logger.warning("dropping malformed log frame for job=%s: %r", job_id, frame)
            return
        await record_log_line(
            session, job_id=job_id, lease_id=lease_id, stream=stream, line=line, ts=ts
        )
        await session.commit()
        return

    if frame_type == "metric":
        epoch = frame.get("epoch")
        loss = frame.get("loss")
        test_accuracy = frame.get("test_accuracy")
        if not isinstance(epoch, int) or not isinstance(loss, int | float):
            logger.warning("dropping malformed metric frame for job=%s: %r", job_id, frame)
            return
        if test_accuracy is not None and not isinstance(test_accuracy, int | float):
            logger.warning("dropping malformed metric frame for job=%s: %r", job_id, frame)
            return
        await record_metric(
            session,
            job_id=job_id,
            lease_id=lease_id,
            epoch=epoch,
            loss=float(loss),
            test_accuracy=float(test_accuracy) if test_accuracy is not None else None,
            ts=ts,
        )
        await session.commit()
        return

    logger.warning("dropping frame with unknown type for job=%s: %r", job_id, frame)


@router.websocket("/nodes/{node_id}/leases/{lease_id}/stream")
async def lease_stream(
    websocket: WebSocket,
    node_id: uuid.UUID,
    lease_id: uuid.UUID,
    epoch: int = Query(...),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> None:
    """Accept the agent's log/metric stream for one lease epoch, or reject it
    (accept, then immediately close with the code from ``OUTCOME_CLOSE_CODES``)."""
    await websocket.accept()

    auth = await authorize_stream(
        session,
        node_id=node_id,
        lease_id=lease_id,
        epoch=epoch,
        authorization_header=websocket.headers.get("authorization"),
        settings=settings,
    )
    if auth.outcome is not StreamAuthOutcome.OK:
        await websocket.close(code=OUTCOME_CLOSE_CODES[auth.outcome], reason=auth.reason)
        return
    assert auth.job_id is not None
    job_id = auth.job_id

    logger.info(
        "stream connected: node=%s lease=%s job=%s epoch=%d", node_id, lease_id, job_id, epoch
    )
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("dropping non-JSON frame for job=%s: %r", job_id, raw)
                continue
            if not isinstance(frame, dict):
                logger.warning("dropping non-object frame for job=%s: %r", job_id, frame)
                continue
            await _persist_frame(session, job_id=job_id, lease_id=lease_id, frame=frame)
    except WebSocketDisconnect:
        logger.info(
            "stream disconnected: node=%s lease=%s job=%s epoch=%d",
            node_id,
            lease_id,
            job_id,
            epoch,
        )
