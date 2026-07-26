"""Adaptive φ-accrual failure detector (ADR-004, M6).

The M1 ``heartbeat_stale`` flag was passive: computed at read time, it never
mutated node status and never triggered anything. This is the real, *active*
detector — a background pass that, per ONLINE node, fits a Normal distribution
to that node's own recent heartbeat inter-arrival intervals, computes a
φ-accrual suspicion level for the current silence, and (crossing a configurable
threshold, never faster than the 5 s hard floor) transitions the node to
OFFLINE and drives immediate recovery.

Two layers, split so the math is unit-testable without a database:

* **Pure math** — :func:`evaluate_suspicion` and the normal-CDF/φ helpers. No
  DB, no wall clock; the only inputs are the observed intervals and the elapsed
  silence. This is what the property test exercises (a ~2 s-cadence node and a
  ~10 s-cadence node reach opposite verdicts at the same 6 s of silence).
* **DB pass** — :func:`run_failure_detection_pass` reads each node's real
  heartbeat receive-times from ``node_telemetry_samples.ts`` (one row per
  heartbeat — real recorded arrivals, not a separate driftable counter),
  evaluates suspicion, and on a declared failure flips status and calls the
  shared :func:`orchestrator.services.leases.reassign_job_attempt` — the *same*
  reassignment code path the TTL sweep uses.

Nothing here fabricates data: a node with too little history to fit a
distribution falls back to an explicit bootstrap silence threshold (documented
config), never a made-up distribution — mirroring ADR-009's reliability prior.

See ``docs/adr/ADR-004-addendum.md`` for the formula, threshold, and the honest
note on the 5 s target vs. the 5 s floor.
"""

from __future__ import annotations

import math
import statistics
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.config import Settings
from orchestrator.core.metrics import failure_detections_total
from orchestrator.models.job import Job
from orchestrator.models.lease import Lease, LeaseState
from orchestrator.models.node import Node, NodeStatus, NodeTelemetrySample
from orchestrator.services.leases import reassign_job_attempt

#: Numerical floor on P_later so φ = -log10(P_later) stays finite as silence → ∞.
_P_LATER_FLOOR = 1e-12


@dataclass(frozen=True)
class PhiAccrualConfig:
    """The detector's tuning, lifted from :class:`Settings` once per pass."""

    threshold: float
    window_samples: int
    min_std_seconds: float
    min_intervals: int
    floor_seconds: float
    bootstrap_silence_seconds: float

    @classmethod
    def from_settings(cls, settings: Settings) -> PhiAccrualConfig:
        return cls(
            threshold=settings.phi_accrual_threshold,
            window_samples=settings.phi_accrual_window_samples,
            min_std_seconds=settings.phi_accrual_min_std_seconds,
            min_intervals=settings.phi_accrual_min_intervals,
            floor_seconds=settings.heartbeat_floor_seconds,
            bootstrap_silence_seconds=settings.phi_accrual_bootstrap_silence_seconds,
        )


@dataclass(frozen=True)
class Suspicion:
    """The verdict on one node at one instant.

    ``phi`` is ``None`` in the bootstrap case (too little history to fit a
    distribution). ``failed`` already incorporates the 5 s hard floor, so it is
    the single authoritative "declare this node failed" bit.
    """

    failed: bool
    elapsed_seconds: float
    phi: float | None
    mean_interval: float | None
    std_interval: float | None
    reason: str


def normal_cdf(x: float) -> float:
    """Standard normal CDF Φ(x), via the error function (no SciPy dependency)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def phi_suspicion(elapsed_seconds: float, mean: float, std: float) -> float:
    """φ = -log10(P(a heartbeat arrives later than ``elapsed_seconds``)), where
    the arrival time is modelled Normal(``mean``, ``std``). Larger ``elapsed`` →
    smaller tail probability → larger φ. ``std`` must already be floored > 0."""
    p_later = 1.0 - normal_cdf((elapsed_seconds - mean) / std)
    return -math.log10(max(p_later, _P_LATER_FLOOR))


def evaluate_suspicion(
    intervals: list[float], elapsed_seconds: float, config: PhiAccrualConfig
) -> Suspicion:
    """Decide whether a node with these recent ``intervals`` and this much
    current ``elapsed_seconds`` of silence should be declared failed.

    Order of reasoning (see ADR-004 addendum):
      1. Too little history (< ``min_intervals``) → bootstrap: failed iff silence
         ≥ ``bootstrap_silence_seconds`` (and ≥ the floor). No distribution is
         invented.
      2. Otherwise fit Normal(μ, σ) over the last ``window_samples`` intervals,
         σ floored at ``min_std_seconds``; failed iff φ ≥ ``threshold`` AND
         silence ≥ the 5 s floor. The floor is applied last, so it can only
         delay a declaration, never accelerate one.
    """
    if len(intervals) < config.min_intervals:
        failed = (
            elapsed_seconds >= config.bootstrap_silence_seconds
            and elapsed_seconds >= config.floor_seconds
        )
        reason = (
            f"{elapsed_seconds:.1f}s of silence with only {len(intervals)} "
            f"interval(s) of history (< {config.min_intervals}); "
            + (
                f"exceeds the {config.bootstrap_silence_seconds:.1f}s bootstrap threshold"
                if failed
                else f"under the {config.bootstrap_silence_seconds:.1f}s bootstrap threshold"
            )
        )
        return Suspicion(
            failed=failed,
            elapsed_seconds=elapsed_seconds,
            phi=None,
            mean_interval=None,
            std_interval=None,
            reason=reason,
        )

    window = intervals[-config.window_samples :]
    mean = statistics.fmean(window)
    std = max(statistics.pstdev(window), config.min_std_seconds)
    phi = phi_suspicion(elapsed_seconds, mean, std)
    failed = phi >= config.threshold and elapsed_seconds >= config.floor_seconds
    reason = (
        f"φ-accrual suspicion {phi:.1f} "
        f"{'≥' if phi >= config.threshold else '<'} {config.threshold:.1f} "
        f"after {elapsed_seconds:.1f}s of silence "
        f"(mean interval {mean:.2f}s, std {std:.2f}s)"
    )
    return Suspicion(
        failed=failed,
        elapsed_seconds=elapsed_seconds,
        phi=phi,
        mean_interval=mean,
        std_interval=std,
        reason=reason,
    )


@dataclass(frozen=True)
class FailureDeclaration:
    """One node declared failed in a pass, plus the jobs it triggered reassigning."""

    node_id: uuid.UUID
    node_name: str
    suspicion: Suspicion
    reassigned_job_ids: list[uuid.UUID]


async def _recent_intervals(
    session: AsyncSession, node_id: uuid.UUID, *, window: int
) -> list[float]:
    """Inter-arrival intervals (seconds) between this node's most-recent
    heartbeats, oldest→newest, from the real ``node_telemetry_samples.ts`` rows.
    At most ``window`` intervals (needs ``window + 1`` samples)."""
    rows = (
        (
            await session.execute(
                select(NodeTelemetrySample.ts)
                .where(NodeTelemetrySample.node_id == node_id)
                .order_by(
                    NodeTelemetrySample.ts.desc(), NodeTelemetrySample.id.desc()
                )
                .limit(window + 1)
            )
        )
        .scalars()
        .all()
    )
    ordered = list(reversed(rows))  # ascending in time
    return [
        (ordered[i] - ordered[i - 1]).total_seconds() for i in range(1, len(ordered))
    ]


async def _reassign_node_jobs(
    session: AsyncSession, *, node: Node, suspicion: Suspicion, now: datetime
) -> list[uuid.UUID]:
    """Immediately reassign every job this just-failed node holds a current-epoch
    lease on, via the shared reassignment path. Returns the reassigned job ids."""
    leases = (
        (
            await session.execute(
                select(Lease)
                .where(
                    Lease.node_id == node.id,
                    Lease.state.in_((LeaseState.ACTIVE, LeaseState.PENDING)),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    by_job: dict[uuid.UUID, list[Lease]] = {}
    for lease in leases:
        by_job.setdefault(lease.job_id, []).append(lease)

    reassigned: list[uuid.UUID] = []
    for job_id, node_leases in by_job.items():
        job = (
            await session.execute(
                select(Job)
                .where(Job.id == job_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        current = [
            lease for lease in node_leases
            if lease.lease_epoch == job.current_lease_epoch
        ]
        if not current:
            continue
        did = await reassign_job_attempt(
            session,
            job=job,
            failed_leases=current,
            now=now,
            message=(
                f"{node.name} stopped responding ({suspicion.reason}); its lease "
                "was torn down and the job requeued for reassignment."
            ),
            extra={
                "trigger": "phi-accrual-detector",
                "node_id": str(node.id),
                "node_name": node.name,
                "phi": suspicion.phi,
                "elapsed_seconds": round(suspicion.elapsed_seconds, 3),
                "lease_epoch": job.current_lease_epoch,
                "failed_ranks": sorted(lease.rank for lease in current),
            },
        )
        if did:
            reassigned.append(job_id)
    return reassigned


async def run_failure_detection_pass(
    session: AsyncSession, *, settings: Settings
) -> list[FailureDeclaration]:
    """Evaluate every ONLINE node's liveness and act on declared failures.

    For each ONLINE node past the 5 s floor of silence, compute the φ-accrual
    suspicion from its own recent heartbeat intervals; on a declared failure,
    flip the node OFFLINE and reassign its jobs through the shared code path.
    Only ONLINE nodes are considered and only an ONLINE→OFFLINE flip acts, so an
    already-declared node is never re-processed and a benign single missed tick
    under the floor is never acted on. Nodes locked ``SKIP LOCKED`` so a node
    mid-heartbeat (clearly alive) is simply skipped this tick. Caller commits.
    """
    config = PhiAccrualConfig.from_settings(settings)
    now = datetime.now(UTC)

    nodes = (
        (
            await session.execute(
                select(Node)
                .where(Node.status == NodeStatus.ONLINE)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )

    declarations: list[FailureDeclaration] = []
    for node in nodes:
        if node.last_heartbeat_at is None:
            continue  # ONLINE is only ever set by a heartbeat; defensive.
        elapsed = (now - node.last_heartbeat_at).total_seconds()
        if elapsed < config.floor_seconds:
            continue  # cannot fail before the hard floor — skip the interval query.

        intervals = await _recent_intervals(
            session, node.id, window=config.window_samples
        )
        suspicion = evaluate_suspicion(intervals, elapsed, config)
        if not suspicion.failed:
            continue

        node.status = NodeStatus.OFFLINE
        reassigned = await _reassign_node_jobs(
            session, node=node, suspicion=suspicion, now=now
        )
        declarations.append(
            FailureDeclaration(
                node_id=node.id,
                node_name=node.name,
                suspicion=suspicion,
                reassigned_job_ids=reassigned,
            )
        )
        failure_detections_total.inc()

    await session.flush()
    return declarations
