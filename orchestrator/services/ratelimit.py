"""Fixed-window rate limiting for credential endpoints (ADR-012 §7).

Guards the four places where an attacker can guess a secret: password login,
the node challenge/refresh pair, and node registration. Everything else is
already gated by a token that has to be obtained through one of these.

**This is in-process state.** Two orchestrator replicas would each allow the
full limit, so the effective limit is per-replica, not per-fleet. ADR-010
deploys exactly one replica, which is why this is correct for the topology we
actually have; the fix when that changes is a shared store (Redis), not a
smaller number here. Recorded rather than hidden, because a rate limiter that
silently does a fraction of what its name implies is worse than none.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of one rate-limit check."""

    allowed: bool
    #: Seconds until the current window closes. Sent as ``Retry-After`` on a
    #: rejection so a legitimate client backs off correctly instead of
    #: hammering, and 0 when allowed.
    retry_after_seconds: int


class FixedWindowRateLimiter:
    """Counts requests per key within a fixed wall-clock window.

    Fixed-window rather than sliding: it is exact under concurrency with one
    lock, and its known weakness (up to 2x the limit across a window boundary)
    is irrelevant against brute force, where the attacker needs orders of
    magnitude more attempts than the limit, not twice as many.

    Uses ``time.monotonic`` so a system clock adjustment cannot extend or
    collapse a window.
    """

    #: Once this many distinct keys are tracked, a check first drops elapsed
    #: windows. Bounds memory against an attacker rotating source addresses
    #: without a background sweeper to forget.
    _PRUNE_THRESHOLD = 4096

    def __init__(self, *, limit: int, window_seconds: float) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._limit = limit
        self._window = window_seconds
        # key -> (window_started_at_monotonic, count_in_window)
        self._windows: dict[str, tuple[float, int]] = {}
        # The API is sync and called from FastAPI's threadpool as well as the
        # event loop; a plain lock keeps the read-modify-write atomic in both.
        self._lock = threading.RLock()

    def check(self, key: str, *, now: float | None = None) -> RateLimitDecision:
        """Record an attempt for ``key`` and decide whether to allow it.

        ``now`` is injectable so tests can drive window expiry deterministically
        instead of sleeping.
        """
        current = time.monotonic() if now is None else now
        if len(self._windows) >= self._PRUNE_THRESHOLD:
            self.prune(now=current)
        with self._lock:
            started, count = self._windows.get(key, (current, 0))
            if current - started >= self._window:
                started, count = current, 0
            count += 1
            self._windows[key] = (started, count)
            if count > self._limit:
                remaining = self._window - (current - started)
                # Round up: reporting 0 would invite an immediate retry that is
                # still inside the window.
                return RateLimitDecision(
                    allowed=False, retry_after_seconds=max(1, int(remaining) + 1)
                )
        return RateLimitDecision(allowed=True, retry_after_seconds=0)

    def reset(self) -> None:
        """Drop all window state. For test isolation, not production use."""
        with self._lock:
            self._windows.clear()

    def prune(self, *, now: float | None = None) -> int:
        """Drop windows that have fully elapsed; return how many were removed.

        Without this the dict grows one entry per distinct client IP forever,
        which on a public-facing port is an unbounded-memory path.
        """
        current = time.monotonic() if now is None else now
        with self._lock:
            stale = [
                key
                for key, (started, _) in self._windows.items()
                if current - started >= self._window
            ]
            for key in stale:
                del self._windows[key]
        return len(stale)
