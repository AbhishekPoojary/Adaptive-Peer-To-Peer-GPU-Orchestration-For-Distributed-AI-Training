"""Reliability math for the adaptive scheduler (ADR-009): Wilson score lower
bound over a node's real lease history, with exponential time decay.

Everything here is pure arithmetic over values the caller has already pulled
from the database — no DB access, no wall-clock reads. That keeps the estimator
unit-testable with hand-computed expectations and keeps the "reliability is
derived from recorded lease history, never assigned" rule (CONTRIBUTING.md #3)
a property of one small, reviewable function.

Two ideas combine:

* **Wilson lower bound.** Given weighted success/failure pseudo-counts, we take
  the *lower* bound of the Wilson score interval on the success probability.
  Unlike a raw success ratio, it is conservative when the sample is small: a
  node with almost no history scores low (uncertain), not optimistically high.
  A brand-new node carries only its declared Beta prior (default Beta(1, 1)),
  which the lower bound maps to a low/neutral value — never a fabricated
  "assume reliable" default.
* **Time decay.** Each historical outcome is weighted by an exponential decay
  in its age, with a configurable half-life, so a failure from a month ago
  weighs far less than one from a minute ago and reliability recovers over time.
"""

from __future__ import annotations

import math


def decay_weight(age_seconds: float, half_life_seconds: float) -> float:
    """Exponential recency weight in ``(0, 1]`` for an outcome ``age_seconds`` old.

    ``0.5 ** (age / half_life)``: weight 1.0 at age 0, 0.5 after one half-life,
    0.25 after two, and so on. A negative age (a timestamp fractionally in the
    future from clock skew) is clamped to 0 so it never exceeds 1.0. A
    non-positive half-life disables decay (every outcome keeps full weight) —
    the caller validates configuration; this stays total.
    """
    if half_life_seconds <= 0.0:
        return 1.0
    age = max(0.0, age_seconds)
    return math.pow(0.5, age / half_life_seconds)


def wilson_lower_bound(successes: float, failures: float, z: float) -> float:
    """Lower bound of the Wilson score interval on the success probability.

    ``successes`` / ``failures`` are (possibly fractional, decay-weighted)
    pseudo-counts that already include the node's declared prior, so the total
    ``n = successes + failures`` is > 0 in every real call. ``z`` is the normal
    quantile for the desired confidence (e.g. 1.96 ≈ 95%). The result is clamped
    to ``[0, 1]`` to absorb floating-point dust at the boundaries.

    With more evidence the interval tightens and the bound rises toward the
    observed ratio; with little evidence it sits well below it. That is exactly
    the "conservative when unproven" behaviour ADR-009 relies on.
    """
    n = successes + failures
    if n <= 0.0:
        return 0.0
    p = successes / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    center = p + z2 / (2.0 * n)
    margin = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n)
    lower = (center - margin) / denominator
    return max(0.0, min(1.0, lower))
