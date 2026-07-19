"""Round-trip-time measurement and agent-side EWMA smoothing.

CONTRIBUTING.md #3: latency is measured RTT, never a constant. The agent
measures the wall-clock round trip of its own heartbeat HTTP call using the
monotonic clock (immune to system-clock adjustments), then smooths the
sequence of real measurements with an exponentially-weighted moving average.

Because a heartbeat's own round-trip time is only known *after* the response
arrives, it cannot be included in the request that produced it — the first
heartbeat a freshly (re)started agent sends therefore reports ``rtt_ms:
null``; every subsequent heartbeat reports the EWMA of round trips measured
so far.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class RttEwma:
    """Exponentially-weighted moving average over measured RTT samples.

    The first observation seeds the average exactly (nothing to blend with
    yet); every later observation blends in at weight ``alpha``. ``value`` is
    ``None`` until :meth:`observe` has been called at least once — never a
    fabricated starting RTT.
    """

    alpha: float
    value: float | None = field(default=None)

    def observe(self, rtt_ms: float) -> float:
        """Fold one real RTT measurement into the average and return it."""
        if self.value is None:
            self.value = rtt_ms
        else:
            self.value = self.alpha * rtt_ms + (1.0 - self.alpha) * self.value
        return self.value


class Stopwatch:
    """Measures elapsed wall time around a block using the monotonic clock.

    Usage::

        with Stopwatch() as sw:
            await client.post(...)
        elapsed = sw.elapsed_ms  # real measured duration, in milliseconds
    """

    def __init__(self) -> None:
        self._start: float | None = None
        self.elapsed_ms: float | None = None

    def __enter__(self) -> Stopwatch:
        self._start = time.monotonic()
        self.elapsed_ms = None
        return self

    def __exit__(self, *exc_info: object) -> None:
        assert self._start is not None
        self.elapsed_ms = (time.monotonic() - self._start) * 1000.0
