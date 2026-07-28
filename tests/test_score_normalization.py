"""Normalization must preserve the *magnitude* of a difference (ADR-009 addendum).

Regression tests for a real defect the M9 benchmark caught in production code.

The adaptive scorer used plain min-max normalization on the load and latency
terms: it divided by the observed spread alone, which rescales whatever
difference happens to exist to the full 0..1 range no matter how small it is.
Two nodes 7 ms apart on loopback scored 0.0 and 1.0 — exactly as two nodes
500 ms apart would. That amplified noise then outvoted a genuine reliability
gap, and the adaptive scheduler placed 3 of 6 jobs on a node with 3 recorded
failures and 0 successes, indistinguishable from round-robin. Measured, not
theorised: ``bench/report/20260728T154205-reliability_placement.json``.

These tests pin the fixed behaviour directly at the pure scoring function, so
the property is checked without needing a fleet.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import pytest

from orchestrator.schedulers.adaptive import (
    DEFAULT_LATENCY_SIGNIFICANT_SPREAD_MS,
    DEFAULT_LOAD_SIGNIFICANT_SPREAD,
    _normalize_unknown_worst,
)


def _norm(values: Sequence[float | None], spread: float = 50.0) -> list[float]:
    return _normalize_unknown_worst(list(values), significant_spread=spread)


# --- The defect ---------------------------------------------------------------


def test_a_trivial_difference_produces_a_trivial_penalty() -> None:
    """The bug, stated as a test. 7 ms apart against a 50 ms significant spread
    is 0.15 of the scale — not 1.0."""
    lo, hi = _norm([52.78, 60.27])
    assert lo == 0.0
    assert math.isclose(hi, 7.49 / 50.0, abs_tol=1e-6)
    assert hi < 0.2, "a noise-level gap must not become a full-scale penalty"


def test_a_gap_is_never_amplified_beyond_its_true_proportion() -> None:
    """The invariant that actually matters: a difference of ``g`` can never
    score higher than ``g / significant_spread``.

    A wider pool may legitimately scale a small gap *down* — 10 ms really is
    minor next to a node 480 ms away — so the bound is one-sided. What plain
    min-max did, and what is now impossible, is scale it *up*.
    """
    for pool in ([20.0, 30.0], [20.0, 30.0, 500.0], [20.0, 30.0, 21.0]):
        scores = _norm(pool)
        gap_score = scores[1] - scores[0]
        assert gap_score <= 10.0 / 50.0 + 1e-9, f"gap amplified in pool {pool}"


def test_a_large_gap_still_reaches_full_scale() -> None:
    """The fix must not blunt real differences: once the observed spread
    exceeds the significant spread, the worst candidate is still 1.0."""
    scores = _norm([10.0, 510.0])
    assert scores == [0.0, 1.0]


def test_no_discontinuity_between_equal_and_nearly_equal() -> None:
    """Plain min-max had a cliff: all-equal mapped to 0.0, but a microsecond of
    difference mapped to 0.0 and 1.0 — an infinitesimal change flipping the
    term from inert to maximal."""
    equal = _norm([40.0, 40.0])
    barely = _norm([40.0, 40.000001])
    assert equal == [0.0, 0.0]
    assert math.isclose(barely[1], 0.0, abs_tol=1e-6)


# --- Behaviour that must be preserved ----------------------------------------


def test_unmeasured_is_still_worst_case() -> None:
    """Rule 2: unknown telemetry is never treated as "fast" or "idle"."""
    assert _norm([10.0, None, 20.0]) == [0.0, 1.0, pytest.approx(0.2)]


def test_all_unknown_is_all_worst() -> None:
    assert _norm([None, None]) == [1.0, 1.0]


def test_ordering_is_preserved() -> None:
    scores = _norm([80.0, 20.0, 50.0])
    assert scores[1] < scores[2] < scores[0]


def test_a_nonpositive_significant_spread_is_rejected() -> None:
    """Zero would reintroduce the division-by-observed-spread bug (and a
    ZeroDivisionError when all values are equal)."""
    with pytest.raises(ValueError, match="significant_spread"):
        _normalize_unknown_worst([1.0, 2.0], significant_spread=0.0)


# --- The shipped defaults -----------------------------------------------------


# The exact per-candidate inputs recorded for adaptive trials 4-6 of
# bench/report/20260728T154205-reliability_placement.json, where the scheduler
# chose the node with 3 recorded failures. Load was *identical* (both agents
# read the same host CPU); only RTT differed, by 7.5 ms of loopback jitter.
_TRIAL_LOAD = [17.0, 17.0]          # healthy, degraded
_TRIAL_RTT_MS = [60.27, 52.78]      # healthy, degraded
_TRIAL_RELIABILITY = [0.3006, 0.0362]  # measured Wilson bounds from that run


def _s_scores(
    l_scores: Sequence[float], d_scores: Sequence[float]
) -> tuple[float, float]:
    """S_i for (healthy, degraded) under the shipped weights a=1, b=1, g=0.5."""
    healthy, degraded = (
        1.0 * load - 1.0 * rel + 0.5 * dist
        for load, rel, dist in zip(
            l_scores, _TRIAL_RELIABILITY, d_scores, strict=True
        )
    )
    return healthy, degraded


def test_loopback_jitter_cannot_outvote_a_real_reliability_gap() -> None:
    """The failing benchmark case, replayed against the shipped defaults.

    Reliability R=0.3006 vs R=0.0362 is a real, earned difference. The 7.5 ms
    RTT gap is noise between two agents on one laptop. The healthy node must
    now win.
    """
    s_healthy, s_degraded = _s_scores(
        _norm(_TRIAL_LOAD, spread=DEFAULT_LOAD_SIGNIFICANT_SPREAD),
        _norm(_TRIAL_RTT_MS, spread=DEFAULT_LATENCY_SIGNIFICANT_SPREAD_MS),
    )
    assert s_healthy < s_degraded, (
        f"healthy S={s_healthy:.4f} must beat degraded S={s_degraded:.4f}; "
        f"this is the regression the M9 benchmark caught"
    )


def test_under_the_old_minmax_the_degraded_node_would_have_won() -> None:
    """Proves the test above is not vacuous, by replaying the same inputs
    through the old pure min-max: the amplified 7.5 ms flipped the decision."""

    def old_minmax(values: list[float]) -> list[float]:
        lo, hi = min(values), max(values)
        spread = hi - lo
        return [0.0 if spread <= 0 else (v - lo) / spread for v in values]

    s_healthy, s_degraded = _s_scores(
        old_minmax(_TRIAL_LOAD), old_minmax(_TRIAL_RTT_MS)
    )
    assert s_degraded < s_healthy, (
        "expected the old min-max to pick the node with 3 recorded failures — "
        "if this no longer holds, the regression scenario has changed"
    )
