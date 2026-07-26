"""φ-accrual failure-detector math (ADR-004 addendum, M6).

Pure unit tests — no DB, no wall clock. The headline property test proves the
threshold is *relative to observed behaviour*, not a fixed number: a node whose
heartbeats arrive every ~2 s and a node whose heartbeats arrive every ~10 s
reach opposite verdicts at the *same* 6 s of silence.
"""

from __future__ import annotations

import math

from orchestrator.services.failure_detection import (
    PhiAccrualConfig,
    evaluate_suspicion,
    normal_cdf,
    phi_suspicion,
)

# Shipped defaults (mirrors config.py), used unless a test overrides one.
_CONFIG = PhiAccrualConfig(
    threshold=3.0,
    window_samples=20,
    min_std_seconds=0.5,
    min_intervals=3,
    floor_seconds=5.0,
    bootstrap_silence_seconds=10.0,
)


def test_normal_cdf_matches_known_values() -> None:
    assert math.isclose(normal_cdf(0.0), 0.5, abs_tol=1e-9)
    # ~68% within ±1σ, ~95% within ±2σ.
    assert math.isclose(normal_cdf(1.0) - normal_cdf(-1.0), 0.6827, abs_tol=1e-3)
    assert math.isclose(normal_cdf(2.0) - normal_cdf(-2.0), 0.9545, abs_tol=1e-3)


def test_phi_rises_monotonically_with_silence() -> None:
    mean, std = 2.0, 0.5
    phis = [phi_suspicion(t, mean, std) for t in (2.0, 3.0, 4.0, 5.0, 6.0)]
    assert phis == sorted(phis)  # non-decreasing
    # At the mean, tail prob is 0.5 → φ ≈ log10(2) ≈ 0.3.
    assert math.isclose(phi_suspicion(mean, mean, std), math.log10(2.0), abs_tol=1e-6)


# --- THE property test: adaptive, not a fixed threshold ----------------------


def test_threshold_is_relative_to_each_nodes_own_cadence() -> None:
    """At the SAME 6 s of silence, a fast-cadence node is suspected and a
    slow-cadence node is not — because φ is computed against each node's own
    recent interval distribution, not a single global timeout."""
    fast = [2.0] * 20  # a node that heartbeats every ~2 s
    slow = [10.0] * 20  # a node that heartbeats every ~10 s

    fast_verdict = evaluate_suspicion(fast, elapsed_seconds=6.0, config=_CONFIG)
    slow_verdict = evaluate_suspicion(slow, elapsed_seconds=6.0, config=_CONFIG)

    assert fast_verdict.phi is not None and slow_verdict.phi is not None
    # The fast node is way past its normal cadence → high φ → failed.
    assert fast_verdict.phi >= _CONFIG.threshold
    assert fast_verdict.failed is True
    # The slow node hasn't even reached its mean interval → ~0 φ → not failed.
    assert slow_verdict.phi < _CONFIG.threshold
    assert slow_verdict.failed is False
    # And concretely: the fast node's suspicion is far higher for identical silence.
    assert fast_verdict.phi > slow_verdict.phi + 5.0


def test_jittery_node_gets_a_more_tolerant_effective_threshold() -> None:
    """Two nodes with the same ~2 s mean but different jitter: at 6 s the steady
    node trips and the jittery one does not — ADR-004's "don't flap jittery
    nodes" property."""
    steady = [2.0] * 20
    jittery = [0.5, 3.5, 1.0, 4.0, 0.8, 3.2, 1.5, 3.8, 0.6, 3.6] * 2  # mean ~2, big std
    steady_v = evaluate_suspicion(steady, elapsed_seconds=6.0, config=_CONFIG)
    jittery_v = evaluate_suspicion(jittery, elapsed_seconds=6.0, config=_CONFIG)
    assert steady_v.failed is True
    assert jittery_v.failed is False
    assert jittery_v.std_interval is not None and steady_v.std_interval is not None
    assert jittery_v.std_interval > steady_v.std_interval


# --- The 5 s hard floor ------------------------------------------------------


def test_hard_floor_prevents_declaration_before_five_seconds() -> None:
    """Even a node whose φ math is astronomically high is never declared failed
    with under 5 s of silence (ADR-004's hand-set safety bound)."""
    fast = [1.0] * 20  # φ at 4.9 s would be enormous
    v = evaluate_suspicion(fast, elapsed_seconds=4.9, config=_CONFIG)
    assert v.phi is not None and v.phi > _CONFIG.threshold  # math says fail...
    assert v.failed is False  # ...but the floor holds it back.
    # One tick later, past the floor, it fails.
    v2 = evaluate_suspicion(fast, elapsed_seconds=5.01, config=_CONFIG)
    assert v2.failed is True


def test_min_std_floor_prevents_over_sensitivity_on_constant_intervals() -> None:
    """Perfectly constant intervals would give σ=0 (φ=∞ for any t>μ); the
    min-std floor keeps the fitted σ at least ``min_std_seconds``."""
    constant = [2.0] * 20
    v = evaluate_suspicion(constant, elapsed_seconds=6.0, config=_CONFIG)
    assert v.std_interval == _CONFIG.min_std_seconds


# --- Bootstrap: too little history -------------------------------------------


def test_bootstrap_used_when_history_too_thin() -> None:
    """With fewer than ``min_intervals`` intervals there is no distribution to
    fit; the detector falls back to the bootstrap silence threshold (never a
    fabricated distribution)."""
    # Only 1 interval (< min_intervals=3): φ is None (bootstrap path).
    thin = [2.0]
    below = evaluate_suspicion(thin, elapsed_seconds=8.0, config=_CONFIG)
    assert below.phi is None
    assert below.failed is False  # 8 s < 10 s bootstrap threshold
    above = evaluate_suspicion(thin, elapsed_seconds=10.5, config=_CONFIG)
    assert above.phi is None
    assert above.failed is True  # 10.5 s ≥ 10 s bootstrap threshold


def test_bootstrap_still_respects_the_floor() -> None:
    cfg = PhiAccrualConfig(
        threshold=3.0,
        window_samples=20,
        min_std_seconds=0.5,
        min_intervals=3,
        floor_seconds=5.0,
        bootstrap_silence_seconds=2.0,  # deliberately below the floor
    )
    # Even though 3 s > the (misconfigured) 2 s bootstrap, the 5 s floor wins.
    v = evaluate_suspicion([2.0], elapsed_seconds=3.0, config=cfg)
    assert v.failed is False
