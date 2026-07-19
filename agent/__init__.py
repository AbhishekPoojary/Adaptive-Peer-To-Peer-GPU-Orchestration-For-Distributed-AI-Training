"""GPU agent package: runs on each peer node, dials out to the orchestrator.

Package skeleton only in M0 — telemetry collection (NVML/psutil), the
runtime (job execution, isolation), and the WebSocket heartbeat client
are implemented in later milestones (see docs/adr/ADR-002, ADR-007).
"""
