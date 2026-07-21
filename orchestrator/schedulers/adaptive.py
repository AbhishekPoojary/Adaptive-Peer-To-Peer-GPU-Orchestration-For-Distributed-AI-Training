"""Adaptive penalty-score scheduler (ADR-009).

Ranks the nodes that survived the shared hard filters by

    ``S_i = α·L_i − β·R_i + γ·D_i``

and picks the **minimum** ``S_i`` — lower load, higher reliability, and lower
latency all pull the score down, toward selection. (Note the sign: ``β``
*subtracts* reliability, so a more reliable node has a lower, better score.)

* ``L_i`` — normalized load in ``[0, 1]``. Real latest utilization (GPU EWMA if
  the node has a GPU, else CPU percent; see ``base.current_load``), min-max
  scaled across *this decision's* eligible pool.
* ``R_i`` — reliability in ``[0, 1]``: the Wilson lower bound over the node's
  decay-weighted lease history (see ``reliability``). Computed upstream (needs
  the DB) and handed in as weighted success/failure pseudo-counts.
* ``D_i`` — normalized latency in ``[0, 1]``. Real ``rtt_ewma_ms`` from the
  latest sample, min-max scaled across the pool.

The scoring here is a pure function over values the caller has already gathered:
no DB, no clock. The DB work (lease history) and the audit-log write live in
``services.scheduling``, which calls :func:`score_candidates`. This module owns
the *math*; the service owns the *I/O*.

Anti-fabrication (CONTRIBUTING.md 1-3) shows up in the normalization:

* A node with **no telemetry sample** has unknown load — normalized to the
  worst case ``1.0``, never assumed idle.
* A node with **no RTT measurement yet** has unknown latency — normalized to
  the worst case ``1.0``, never assumed low-latency.
* When the pool gives no basis for comparison (a single candidate, or every
  candidate identical), a *known* value normalizes to ``0.0`` — there is no
  relative signal to extract, so it neither helps nor hurts.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.models.job import Job
from orchestrator.schedulers.base import NodeSnapshot, current_load
from orchestrator.schedulers.reliability import wilson_lower_bound


@dataclass(frozen=True)
class CandidateReliability:
    """A hard-filter survivor plus its decay-weighted reliability pseudo-counts.

    ``weighted_success`` / ``weighted_failure`` already fold in the node's
    declared prior and the time-decayed lease outcomes; the scorer only turns
    them into a Wilson bound. Assembled by ``services.scheduling`` from real
    ``Lease`` rows.
    """

    snapshot: NodeSnapshot
    weighted_success: float
    weighted_failure: float


@dataclass(frozen=True)
class ScoredCandidate:
    """One candidate's fully-computed score plus the raw inputs behind it.

    Carries everything the audit log needs to reconstruct *why* a node scored
    as it did: the raw utilization and RTT that fed normalization, the weighted
    counts that fed reliability, the three component scores, and the final
    ``s_score``. ``was_selected`` is stamped by the caller after the winner is
    chosen.
    """

    snapshot: NodeSnapshot
    raw_util: float | None
    raw_rtt_ewma_ms: float | None
    weighted_success: float
    weighted_failure: float
    l_score: float
    r_score: float
    d_score: float
    s_score: float
    was_selected: bool


def _latest_rtt_ewma(snapshot: NodeSnapshot) -> float | None:
    """The node's smoothed RTT from its latest sample, or ``None`` if unmeasured.

    ``None`` covers both "no telemetry sample at all" and "sample present but
    RTT never measured" — both are genuinely-unknown latency, treated as
    worst-case by the normalizer, never as fast.
    """
    sample = snapshot.latest_sample
    if sample is None:
        return None
    return sample.rtt_ewma_ms


def _normalize_unknown_worst(values: list[float | None]) -> list[float]:
    """Min-max normalize ``values`` to ``[0, 1]``; ``None`` becomes worst (1.0).

    Known values are scaled against the min/max of the *known* values in the
    pool. A ``None`` (unmeasured) is mapped to ``1.0`` — the worst case — so an
    unmeasured node is never preferred over a measured one on this axis. When
    there is no spread among the known values (a single known value, or all
    equal), every known value maps to ``0.0``: no relative comparison is
    possible, documented per ADR-009.
    """
    known = [v for v in values if v is not None]
    lo = min(known) if known else 0.0
    hi = max(known) if known else 0.0
    spread = hi - lo
    out: list[float] = []
    for v in values:
        if v is None:
            out.append(1.0)
        elif spread <= 0.0:
            out.append(0.0)
        else:
            out.append((v - lo) / spread)
    return out


def score_candidates(
    candidates: list[CandidateReliability],
    *,
    alpha: float,
    beta: float,
    gamma: float,
    wilson_z: float,
) -> list[ScoredCandidate]:
    """Score every candidate by ``S_i = α·L_i − β·R_i + γ·D_i`` (pure).

    Load and latency are min-max normalized across exactly this candidate pool,
    so the scores are only comparable *within one decision* — which is all the
    selection needs. Returns one ``ScoredCandidate`` per input, in input order,
    all with ``was_selected=False`` (the caller stamps the winner). An empty
    input yields an empty list.
    """
    raw_utils = [current_load(c.snapshot) for c in candidates]
    raw_rtts = [_latest_rtt_ewma(c.snapshot) for c in candidates]
    l_scores = _normalize_unknown_worst(raw_utils)
    d_scores = _normalize_unknown_worst(raw_rtts)

    scored: list[ScoredCandidate] = []
    for cand, raw_util, raw_rtt, l_i, d_i in zip(
        candidates, raw_utils, raw_rtts, l_scores, d_scores, strict=True
    ):
        r_i = wilson_lower_bound(cand.weighted_success, cand.weighted_failure, wilson_z)
        s_i = alpha * l_i - beta * r_i + gamma * d_i
        scored.append(
            ScoredCandidate(
                snapshot=cand.snapshot,
                raw_util=raw_util,
                raw_rtt_ewma_ms=raw_rtt,
                weighted_success=cand.weighted_success,
                weighted_failure=cand.weighted_failure,
                l_score=l_i,
                r_score=r_i,
                d_score=d_i,
                s_score=s_i,
                was_selected=False,
            )
        )
    return scored


def select_best(scored: list[ScoredCandidate]) -> ScoredCandidate | None:
    """Return the minimum-``S_i`` candidate, or ``None`` for an empty pool.

    Ties break deterministically on node name so repeated passes over an
    unchanged pool are stable.
    """
    if not scored:
        return None
    return min(scored, key=lambda c: (c.s_score, c.snapshot.node.name))


class AdaptiveScheduler:
    """The ``adaptive`` strategy's registry entry (ADR-009 penalty score).

    Unlike the ``round_robin``/``least_loaded`` baselines, adaptive placement
    needs the node's real lease history (for reliability) and writes a
    per-decision audit trail — neither of which fits the pure, DB-free
    ``select_node(job, candidates)`` contract the baselines share. So the
    working path lives in ``services.scheduling.place_job_adaptive``, which the
    scheduler pass routes ``adaptive`` jobs through; this class exists so the
    strategy is a first-class registered name (validated at submit, listed in
    the registry, selectable via ``SCHEDULER_STRATEGY``).

    ``select_node`` is intentionally not the placement path: calling it would
    have to either query the DB (breaking the pure contract) or score without
    history (fabricating reliability). It raises instead of doing either — a
    loud failure beats a silently wrong ranking.
    """

    name = "adaptive"

    async def select_node(
        self, job: Job, candidates: list[NodeSnapshot]
    ) -> NodeSnapshot | None:
        raise NotImplementedError(
            "adaptive scheduling is DB-backed and audited: the scheduler pass "
            "routes 'adaptive' jobs through "
            "orchestrator.services.scheduling.place_job_adaptive, not this pure "
            "select_node path. Reliability requires real lease history, which "
            "the NodeSnapshot contract deliberately excludes."
        )
