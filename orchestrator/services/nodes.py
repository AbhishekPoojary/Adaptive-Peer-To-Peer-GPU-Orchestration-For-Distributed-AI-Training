"""Node service: enrollment (token consumption + node creation) and heartbeat.

Registration validates the agent's Ed25519 public key and consumes the
enrollment token in a single transaction the caller commits — so if anything
fails, the token is not burned. Heartbeat records telemetry exactly as reported
and maintains the RTT EWMA from measured RTT only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from orchestrator.core.config import Settings
from orchestrator.core.metrics import heartbeats_received_total
from orchestrator.core.security import load_ed25519_public_key
from orchestrator.models.node import Node, NodeStatus, NodeTelemetrySample
from orchestrator.schemas.node import (
    GpuTelemetry,
    HardwareInventory,
    HeartbeatRequest,
    NodeRegisterRequest,
    NodeSummary,
    TelemetrySampleOut,
)
from orchestrator.services.enrollment import TokenClaimOutcome, claim_enrollment_token


class RegistrationError(Exception):
    """Registration could not be completed; ``outcome`` explains why."""

    def __init__(self, outcome: TokenClaimOutcome) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome


async def register_node(
    session: AsyncSession, *, req: NodeRegisterRequest, settings: Settings
) -> Node:
    """Consume the enrollment token and create the Node. Caller commits.

    Raises :class:`~orchestrator.core.security.PublicKeyError` (bad key, 400) or
    :class:`RegistrationError` (token problem, 401/409). The public key is
    validated *before* the token is consumed so a malformed key never burns a
    token.
    """
    # Validate the key first — fail before touching the token.
    load_ed25519_public_key(req.public_key)

    outcome, _ = await claim_enrollment_token(session, raw_token=req.enrollment_token)
    if outcome is not TokenClaimOutcome.CLAIMED:
        raise RegistrationError(outcome)

    # Server-assigned unique name via a dedicated sequence (race-safe).
    seq_value = (await session.execute(text("SELECT nextval('node_name_seq')"))).scalar_one()
    name = f"node-{int(seq_value):02d}"

    node = Node(
        name=name,
        public_key=req.public_key,
        status=NodeStatus.OFFLINE,
        hardware=req.hardware.model_dump(),
        agent_version=req.agent_version,
        reliability_prior_alpha=settings.reliability_prior_alpha,
        reliability_prior_beta=settings.reliability_prior_beta,
    )
    session.add(node)
    await session.flush()
    await session.refresh(node)
    return node


@dataclass(frozen=True)
class HeartbeatResult:
    """Values the heartbeat handler needs for its response."""

    server_time: datetime
    last_heartbeat_at: datetime
    rtt_ewma_ms: float | None


async def _previous_rtt_ewma(session: AsyncSession, node_id: object) -> float | None:
    """Return the most recent sample's RTT EWMA for a node, or None if no prior.

    The running EWMA state lives in the telemetry table (per the sample schema),
    not on Node — we read it back from the latest sample rather than inventing a
    starting value.
    """
    stmt = (
        select(NodeTelemetrySample.rtt_ewma_ms)
        .where(NodeTelemetrySample.node_id == node_id)
        .order_by(NodeTelemetrySample.ts.desc(), NodeTelemetrySample.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def record_heartbeat(
    session: AsyncSession, *, node: Node, payload: HeartbeatRequest, settings: Settings
) -> HeartbeatResult:
    """Record a heartbeat: insert a telemetry sample, update RTT EWMA, mark the
    node ONLINE and stamp last_heartbeat_at. Caller commits.

    The RTT EWMA is only advanced by a measured ``rtt_ms``. When the agent has
    no RTT to report, the previous EWMA is carried forward unchanged — never a
    fabricated value — and the sample's own ``rtt_ms`` stays NULL.
    """
    received_at = datetime.now(UTC)
    prev_ewma = await _previous_rtt_ewma(session, node.id)

    if payload.rtt_ms is None:
        new_ewma = prev_ewma
    elif prev_ewma is None:
        new_ewma = payload.rtt_ms
    else:
        alpha = settings.rtt_ewma_alpha
        new_ewma = alpha * payload.rtt_ms + (1.0 - alpha) * prev_ewma

    gpu_payload = (
        [g.model_dump() for g in payload.gpu] if payload.gpu is not None else None
    )
    sample = NodeTelemetrySample(
        node_id=node.id,
        cpu_percent=payload.cpu_percent,
        ram_used_bytes=payload.ram_used_bytes,
        ram_total_bytes=payload.ram_total_bytes,
        gpu=gpu_payload,
        rtt_ms=payload.rtt_ms,
        rtt_ewma_ms=new_ewma,
    )
    session.add(sample)

    node.last_heartbeat_at = received_at
    # Receiving a heartbeat is direct evidence the node is reachable right now.
    node.status = NodeStatus.ONLINE
    await session.flush()
    heartbeats_received_total.inc()

    return HeartbeatResult(
        server_time=received_at,
        last_heartbeat_at=received_at,
        rtt_ewma_ms=new_ewma,
    )


# --- Read endpoints (GET /nodes, GET /nodes/{id}) ----------------------------


def _sample_out(sample: NodeTelemetrySample | None) -> TelemetrySampleOut | None:
    """Map an ORM sample row to its schema, or None if there isn't one yet."""
    if sample is None:
        return None
    gpu = (
        [GpuTelemetry.model_validate(g) for g in sample.gpu]
        if sample.gpu is not None
        else None
    )
    return TelemetrySampleOut(
        ts=sample.ts,
        cpu_percent=sample.cpu_percent,
        ram_used_bytes=sample.ram_used_bytes,
        ram_total_bytes=sample.ram_total_bytes,
        gpu=gpu,
        rtt_ms=sample.rtt_ms,
        rtt_ewma_ms=sample.rtt_ewma_ms,
    )


def _is_stale(last_heartbeat_at: datetime | None, *, stale_seconds: float, now: datetime) -> bool:
    """A node that has never heartbeated is trivially stale.

    Otherwise stale iff the gap since the last heartbeat exceeds the
    configured window. Read-time computation only — never mutates status.
    """
    if last_heartbeat_at is None:
        return True
    return (now - last_heartbeat_at) > timedelta(seconds=stale_seconds)


def _to_summary(
    node: Node,
    latest: NodeTelemetrySample | None,
    *,
    stale_seconds: float,
    now: datetime,
) -> NodeSummary:
    return NodeSummary(
        id=node.id,
        name=node.name,
        status=node.status.value,
        last_heartbeat_at=node.last_heartbeat_at,
        heartbeat_stale=_is_stale(node.last_heartbeat_at, stale_seconds=stale_seconds, now=now),
        hardware=HardwareInventory.model_validate(node.hardware),
        lease_success_count=node.lease_success_count,
        lease_failure_count=node.lease_failure_count,
        latest_telemetry=_sample_out(latest),
    )


async def list_nodes(session: AsyncSession, *, settings: Settings) -> list[NodeSummary]:
    """Return every node with its latest telemetry sample (if any).

    A single query fetches the latest-sample-per-node via a Postgres
    ``DISTINCT ON`` derived table joined back to ``nodes`` — avoiding an N+1
    "one query per node" pattern regardless of fleet size.
    """
    latest_subq = (
        select(NodeTelemetrySample)
        .distinct(NodeTelemetrySample.node_id)
        .order_by(
            NodeTelemetrySample.node_id,
            NodeTelemetrySample.ts.desc(),
            NodeTelemetrySample.id.desc(),
        )
        .subquery()
    )
    latest_sample = aliased(NodeTelemetrySample, latest_subq)

    stmt = (
        select(Node, latest_sample)
        .outerjoin(latest_sample, latest_subq.c.node_id == Node.id)
        .order_by(Node.name)
    )
    rows = (await session.execute(stmt)).all()

    now = datetime.now(UTC)
    return [
        _to_summary(node, latest, stale_seconds=settings.heartbeat_stale_seconds, now=now)
        for node, latest in rows
    ]


@dataclass(frozen=True)
class NodeDetail:
    """A node's summary plus its most recent telemetry samples, newest first."""

    summary: NodeSummary
    telemetry_samples: list[TelemetrySampleOut]


async def get_node_detail(
    session: AsyncSession, *, node_id: uuid.UUID, sample_limit: int, settings: Settings
) -> NodeDetail | None:
    """Return one node's detail view, or None if it does not exist.

    ``sample_limit`` is expected to already be capped by the caller (the API
    layer enforces ``node_detail_max_samples``); this just applies it.
    """
    node = await session.get(Node, node_id)
    if node is None:
        return None

    samples_stmt = (
        select(NodeTelemetrySample)
        .where(NodeTelemetrySample.node_id == node_id)
        .order_by(NodeTelemetrySample.ts.desc(), NodeTelemetrySample.id.desc())
        .limit(sample_limit)
    )
    samples = (await session.execute(samples_stmt)).scalars().all()
    latest = samples[0] if samples else None

    now = datetime.now(UTC)
    summary = _to_summary(node, latest, stale_seconds=settings.heartbeat_stale_seconds, now=now)
    return NodeDetail(
        summary=summary,
        # `samples` rows are never None themselves, so _sample_out(s) is never
        # None here — the Optional in its signature is only for the "no
        # latest sample yet" case above.
        telemetry_samples=[out for s in samples if (out := _sample_out(s)) is not None],
    )
