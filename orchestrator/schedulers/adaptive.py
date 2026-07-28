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


#: Smallest utilization gap (percentage points, load is 0..100) treated as a
#: full-scale load difference. Nodes closer than this are "similarly loaded"
#: and the alpha*L term scales their gap down proportionally rather than
#: amplifying it. 25 points is a quarter of the range: enough that ordinary
#: sampling jitter on an idle fleet stays small, while a genuinely busy node
#: (say 80% against 20%) still reaches ~1.0.
DEFAULT_LOAD_SIGNIFICANT_SPREAD = 25.0

#: Smallest RTT gap (ms) treated as a full-scale latency difference. Peers on
#: one LAN or a Tailscale overlay differ by single-digit milliseconds of noise;
#: a peer that is genuinely far differs by tens. 50 ms sits above the noise and
#: below a real WAN hop, so loopback jitter contributes almost nothing while a
#: distant node is properly penalised.
DEFAULT_LATENCY_SIGNIFICANT_SPREAD_MS = 50.0


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


def _normalize_unknown_worst(
    values: list[float | None], *, significant_spread: float
) -> list[float]:
    """Normalize ``values`` to ``[0, 1]``; ``None`` becomes worst (1.0).

    Scaled as ``(v - lo) / max(observed_spread, significant_spread)``, so the
    *magnitude* of a difference survives normalization.

    Plain min-max — which this was until the M9 benchmark — is wrong here, and
    measurably so. It divides by the observed spread alone, which rescales
    whatever difference happens to exist to the full 0..1 range no matter how
    small it is. Two nodes 7 ms apart on loopback scored 0.0 and 1.0, exactly
    as two nodes 500 ms apart would: a full-scale latency penalty for noise.
    In ``bench/report/20260728T154205-reliability_placement.json`` that
    amplified 7 ms outvoted a real reliability gap (R=0.30 vs R=0.04), and the
    adaptive scheduler placed 3/6 jobs on a node with 3 recorded failures and
    0 successes — indistinguishable from round-robin.

    Dividing by at least ``significant_spread`` fixes that: differences smaller
    than what the domain considers meaningful produce proportionally small
    penalties, and only a genuinely large spread reaches 1.0. It also removes a
    discontinuity that plain min-max had — all-equal values mapped to 0.0, but
    values a microsecond apart mapped to 0.0 and 1.0, so an infinitesimal
    change flipped the term from inert to maximal.

    A ``None`` (unmeasured) still maps to ``1.0`` — the worst case — so an
    unmeasured node is never preferred over a measured one on this axis. With
    no known values at all, every entry is unknown and the result is all 1.0.
    """
    if significant_spread <= 0.0:
        raise ValueError("significant_spread must be > 0")
    known = [v for v in values if v is not None]
    lo = min(known) if known else 0.0
    hi = max(known) if known else 0.0
    denominator = max(hi - lo, significant_spread)
    return [1.0 if v is None else (v - lo) / denominator for v in values]


def score_candidates(
    candidates: list[CandidateReliability],
    *,
    alpha: float,
    beta: float,
    gamma: float,
    wilson_z: float,
    load_significant_spread: float = DEFAULT_LOAD_SIGNIFICANT_SPREAD,
    latency_significant_spread_ms: float = DEFAULT_LATENCY_SIGNIFICANT_SPREAD_MS,
) -> list[ScoredCandidate]:
    """Score every candidate by ``S_i = α·L_i − β·R_i + γ·D_i`` (pure).

    Load and latency are normalized across exactly this candidate pool against
    at least the domain's significant spread, so a difference too small to
    matter cannot outvote the other terms (see
    :func:`_normalize_unknown_worst`). Scores remain comparable only *within
    one decision*, which is all the selection needs. Returns one
    ``ScoredCandidate`` per input, in input order, all with
    ``was_selected=False`` (the caller stamps the winner). An empty input
    yields an empty list.
    """
    raw_utils = [current_load(c.snapshot) for c in candidates]
    raw_rtts = [_latest_rtt_ewma(c.snapshot) for c in candidates]
    l_scores = _normalize_unknown_worst(
        raw_utils, significant_spread=load_significant_spread
    )
    d_scores = _normalize_unknown_worst(
        raw_rtts, significant_spread=latency_significant_spread_ms
    )

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


def _rank_key(c: ScoredCandidate) -> tuple[float, str]:
    """Deterministic ranking key: lowest ``S_i`` wins, ties broken on node name
    so repeated passes over an unchanged pool are stable."""
    return (c.s_score, c.snapshot.node.name)


def select_best(scored: list[ScoredCandidate]) -> ScoredCandidate | None:
    """Return the minimum-``S_i`` candidate, or ``None`` for an empty pool.

    Ties break deterministically on node name so repeated passes over an
    unchanged pool are stable.
    """
    if not scored:
        return None
    return min(scored, key=_rank_key)


def select_top_n(scored: list[ScoredCandidate], n: int) -> list[ScoredCandidate]:
    """The ``n`` lowest-``S_i`` candidates, best first (ADR-005 cohort selection).

    The natural generalisation of :func:`select_best` from one winner to a
    world_size=N cohort: sort by the same key and take the first ``n``. Returns
    fewer than ``n`` only when the pool is smaller (the caller must decide
    whether a short pool is placeable — a cohort needs the full N). Ties break
    on node name, so selection is stable across passes.
    """
    return sorted(scored, key=_rank_key)[: max(0, n)]


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

    _DB_BACKED = (
        "adaptive scheduling is DB-backed and audited: the scheduler pass routes "
        "'adaptive' jobs through "
        "orchestrator.services.scheduling.place_job_cohort, not this pure "
        "ranking path. Reliability requires real lease history, which the "
        "NodeSnapshot contract deliberately excludes."
    )

    def rank_candidates(
        self, job: Job, candidates: list[NodeSnapshot]
    ) -> list[NodeSnapshot]:
        raise NotImplementedError(self._DB_BACKED)

    async def select_node(
        self, job: Job, candidates: list[NodeSnapshot]
    ) -> NodeSnapshot | None:
        raise NotImplementedError(self._DB_BACKED)
