"""Pure unit tests for the adaptive scorer's math (ADR-009).

No database: these pin the reliability estimator (Wilson lower bound + time
decay) and the load/latency normalization directly, with hand-computed
expectations. The DB-backed black-box property tests live in
``test_adaptive_scheduler.py``.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta

from orchestrator.models.node import Node, NodeStatus, NodeTelemetrySample
from orchestrator.schedulers.adaptive import (
    CandidateReliability,
    score_candidates,
    select_best,
)
from orchestrator.schedulers.base import NodeSnapshot
from orchestrator.schedulers.reliability import decay_weight, wilson_lower_bound

_NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
_GB = 1024**3

# Beta(1, 1) prior, no history: hand-computed Wilson lower bound at z=1.96.
_PRIOR_ONLY_R = 0.094525


# --- decay_weight ------------------------------------------------------------


def test_decay_weight_is_one_at_zero_age() -> None:
    assert decay_weight(0.0, 100.0) == 1.0


def test_decay_weight_halves_each_half_life() -> None:
    assert decay_weight(100.0, 100.0) == 0.5
    assert decay_weight(200.0, 100.0) == 0.25
    assert math.isclose(decay_weight(300.0, 100.0), 0.125)


def test_decay_weight_clamps_negative_age_to_full_weight() -> None:
    # A timestamp fractionally in the future (clock skew) never exceeds 1.0.
    assert decay_weight(-50.0, 100.0) == 1.0


def test_decay_weight_nonpositive_half_life_disables_decay() -> None:
    assert decay_weight(9999.0, 0.0) == 1.0
    assert decay_weight(9999.0, -1.0) == 1.0


# --- wilson_lower_bound ------------------------------------------------------


def test_wilson_zero_total_is_zero() -> None:
    assert wilson_lower_bound(0.0, 0.0, 1.96) == 0.0


def test_wilson_prior_only_matches_hand_computation() -> None:
    # Beta(1, 1): 1 pseudo-success, 1 pseudo-failure.
    assert math.isclose(wilson_lower_bound(1.0, 1.0, 1.96), _PRIOR_ONLY_R, rel_tol=1e-4)


def test_wilson_success_raises_failure_lowers_relative_to_prior() -> None:
    base = wilson_lower_bound(1.0, 1.0, 1.96)
    with_success = wilson_lower_bound(2.0, 1.0, 1.96)
    with_failure = wilson_lower_bound(1.0, 2.0, 1.96)
    assert with_success > base > with_failure


def test_wilson_more_successes_monotonically_increase_bound() -> None:
    seq = [wilson_lower_bound(1.0 + k, 1.0, 1.96) for k in range(0, 6)]
    assert seq == sorted(seq)
    assert all(0.0 <= v <= 1.0 for v in seq)


def test_wilson_tightens_with_more_evidence_at_same_ratio() -> None:
    # Same 2:1 success ratio, more evidence -> higher (tighter) lower bound.
    small = wilson_lower_bound(2.0, 1.0, 1.96)
    large = wilson_lower_bound(20.0, 10.0, 1.96)
    assert large > small


# --- score_candidates normalization ------------------------------------------


def _node(name: str, *, with_gpu: bool = True) -> Node:
    gpus = (
        [{"name": "T", "vram_bytes": 8 * _GB, "driver_version": "x"}] if with_gpu else []
    )
    return Node(
        id=uuid.uuid4(),
        name=name,
        public_key="pem",
        status=NodeStatus.ONLINE,
        last_heartbeat_at=_NOW - timedelta(seconds=1),
        hardware={"hostname": name, "os": "t", "cpu_model": "t", "cores": 4,
                  "ram_bytes": 16 * _GB, "gpus": gpus},
        agent_version="t",
        reliability_prior_alpha=1.0,
        reliability_prior_beta=1.0,
    )


def _sample(*, cpu: float, gpu_util: float | None, rtt_ewma: float | None) -> NodeTelemetrySample:
    gpu = (
        [{"util_percent": gpu_util, "mem_used_bytes": 0, "mem_total_bytes": _GB,
          "temperature_c": None, "power_w": None}]
        if gpu_util is not None
        else None
    )
    return NodeTelemetrySample(
        cpu_percent=cpu, ram_used_bytes=0, ram_total_bytes=_GB, gpu=gpu,
        rtt_ms=None, rtt_ewma_ms=rtt_ewma,
    )


def _cand(
    name: str,
    *,
    gpu_util: float | None,
    rtt_ewma: float | None,
    ws: float = 1.0,
    wf: float = 1.0,
    has_sample: bool = True,
) -> CandidateReliability:
    node = _node(name)
    sample = _sample(cpu=5.0, gpu_util=gpu_util, rtt_ewma=rtt_ewma) if has_sample else None
    snap = NodeSnapshot(node=node, latest_sample=sample, has_active_lease=False)
    return CandidateReliability(snapshot=snap, weighted_success=ws, weighted_failure=wf)


_P = {"alpha": 1.0, "beta": 1.0, "gamma": 0.5, "wilson_z": 1.96}


def _by_name(scored: list) -> dict[str, object]:
    return {c.snapshot.node.name: c for c in scored}


def test_single_candidate_load_and_latency_normalize_to_zero() -> None:
    # No basis for relative comparison -> L and D are 0 for the lone candidate.
    scored = score_candidates([_cand("only", gpu_util=42.0, rtt_ewma=17.0)], **_P)
    assert scored[0].l_score == 0.0
    assert scored[0].d_score == 0.0
    assert scored[0].raw_util == 42.0
    assert scored[0].raw_rtt_ewma_ms == 17.0


def test_load_minmax_across_pool() -> None:
    scored = _by_name(
        score_candidates(
            [
                _cand("idle", gpu_util=10.0, rtt_ewma=10.0),
                _cand("mid", gpu_util=50.0, rtt_ewma=10.0),
                _cand("busy", gpu_util=90.0, rtt_ewma=10.0),
            ],
            **_P,
        )
    )
    assert scored["idle"].l_score == 0.0
    assert scored["busy"].l_score == 1.0
    assert math.isclose(scored["mid"].l_score, 0.5)
    # Equal RTT across the pool -> no latency spread -> all D = 0.
    assert scored["idle"].d_score == scored["busy"].d_score == 0.0


def test_missing_telemetry_is_worst_case_load_and_latency() -> None:
    scored = _by_name(
        score_candidates(
            [
                _cand("known", gpu_util=20.0, rtt_ewma=30.0),
                _cand("blind", gpu_util=None, rtt_ewma=None, has_sample=False),
            ],
            **_P,
        )
    )
    # The telemetry-less node is worst on both axes, never assumed idle/fast.
    assert scored["blind"].l_score == 1.0
    assert scored["blind"].d_score == 1.0
    assert scored["blind"].raw_util is None
    assert scored["blind"].raw_rtt_ewma_ms is None
    assert scored["known"].l_score == 0.0
    assert scored["known"].d_score == 0.0


def test_null_rtt_is_worst_case_even_with_a_sample() -> None:
    # Sample present (so load is known) but RTT never measured -> D = 1.0.
    scored = _by_name(
        score_candidates(
            [
                _cand("measured", gpu_util=40.0, rtt_ewma=12.0),
                _cand("no_rtt", gpu_util=40.0, rtt_ewma=None),
            ],
            **_P,
        )
    )
    assert scored["no_rtt"].d_score == 1.0
    assert scored["measured"].d_score == 0.0


def test_s_score_formula_sign_and_selection() -> None:
    # Two identical-telemetry nodes differing only in reliability: higher
    # reliability -> lower S -> selected (beta subtracts R).
    scored = score_candidates(
        [
            _cand("reliable", gpu_util=50.0, rtt_ewma=50.0, ws=9.0, wf=1.0),
            _cand("shaky", gpu_util=50.0, rtt_ewma=50.0, ws=1.0, wf=9.0),
        ],
        **_P,
    )
    named = _by_name(scored)
    # Identical telemetry -> L and D both 0 for both -> S = -beta*R.
    assert named["reliable"].l_score == named["shaky"].l_score == 0.0
    assert named["reliable"].r_score > named["shaky"].r_score
    assert named["reliable"].s_score < named["shaky"].s_score
    assert select_best(scored).snapshot.node.name == "reliable"


def test_empty_pool_scores_and_selects_nothing() -> None:
    assert score_candidates([], **_P) == []
    assert select_best([]) is None
