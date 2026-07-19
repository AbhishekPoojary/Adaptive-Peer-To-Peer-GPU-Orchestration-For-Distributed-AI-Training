"""GPU agent package: runs on each peer node, dials out to the orchestrator.

M1: enrollment, challenge-response JWT refresh, and the real telemetry
heartbeat loop (NVML/psutil) are implemented here. Job execution/isolation
(ADR-007) and the WebSocket stream transport for logs/metrics (ADR-002)
remain later-milestone work; the M1 heartbeat uses the REST endpoint the
orchestrator's identity core already exposes.
"""

from __future__ import annotations

__version__ = "0.1.0"
