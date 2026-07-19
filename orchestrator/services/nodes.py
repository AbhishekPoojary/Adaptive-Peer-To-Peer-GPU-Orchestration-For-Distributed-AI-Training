"""Node service: enrollment (token consumption + node creation) and heartbeat.

Registration validates the agent's Ed25519 public key and consumes the
enrollment token in a single transaction the caller commits — so if anything
fails, the token is not burned. Heartbeat records telemetry exactly as reported
and maintains the RTT EWMA from measured RTT only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.config import Settings
from orchestrator.core.security import load_ed25519_public_key
from orchestrator.models.node import Node, NodeStatus, NodeTelemetrySample
from orchestrator.schemas.node import HeartbeatRequest, NodeRegisterRequest
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

    return HeartbeatResult(
        server_time=received_at,
        last_heartbeat_at=received_at,
        rtt_ewma_ms=new_ewma,
    )
