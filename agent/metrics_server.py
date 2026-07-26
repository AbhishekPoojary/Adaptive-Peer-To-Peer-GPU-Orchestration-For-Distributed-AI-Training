"""Agent-side `/metrics` (M7): a small, optional Prometheus HTTP server.

Off by default (no ``--metrics-port`` given). When enabled, exposes real
agent-side values only, sourced from the same NVML/psutil readings the
heartbeat loop already collects — never a second, invented data path:

* ``agent_heartbeats_sent_total`` — real count of heartbeats this process has
  successfully sent.
* ``agent_rtt_ewma_ms`` — the same agent-side RTT EWMA logged every heartbeat
  cycle (:class:`agent.telemetry.latency.RttEwma`). Absent until at least one
  round trip has been measured.
* ``agent_lease_active`` / ``agent_container_running`` — real booleans (as
  0/1 gauges) reflecting whether this node currently holds a lease / has a
  trainer container running for it.
* ``agent_gpu_util_percent`` / ``agent_gpu_mem_used_bytes`` /
  ``agent_gpu_mem_total_bytes`` / ``agent_gpu_temperature_celsius`` — one
  series per GPU index, from the live NVML reading. Emitted only when NVML
  actually reported that GPU this cycle: CONTRIBUTING.md #2 forbids reporting
  a plausible-looking 0 in place of "unknown", so a custom
  :class:`~prometheus_client.registry.Collector` is used instead of plain
  ``Gauge`` objects — a plain Gauge defaults to 0 and would always appear in
  the exposition even when NVML has nothing to say. ``power_w`` and
  ``temperature_c`` are further per-field optional (some drivers don't expose
  them; see ``agent/telemetry/nvml.py``), so each is only emitted when present.

The HTTP server itself runs on a background thread (the standard
``prometheus_client.start_http_server`` mechanism); state is written from the
asyncio event loop thread and read from that background thread, so a plain
``threading.Lock`` guards every access.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass, field

from prometheus_client import CollectorRegistry, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from agent.telemetry.nvml import GpuTelemetryEntry


@dataclass
class AgentMetricsState:
    """Mutable snapshot of this agent's real, current values.

    Every field defaults to "nothing observed yet" (0 / None / False) — the
    honest starting state of a freshly started agent, not an invented one.
    Updated in place by the main loop after each real heartbeat/lease cycle;
    read by :class:`_AgentCollector` on every scrape.
    """

    heartbeats_sent: int = 0
    rtt_ewma_ms: float | None = None
    lease_active: bool = False
    container_running: bool = False
    gpus: list[GpuTelemetryEntry] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record_heartbeat_sent(self, *, rtt_ewma_ms: float | None) -> None:
        with self.lock:
            self.heartbeats_sent += 1
            self.rtt_ewma_ms = rtt_ewma_ms

    def record_lease_state(self, *, lease_active: bool, container_running: bool) -> None:
        with self.lock:
            self.lease_active = lease_active
            self.container_running = container_running

    def record_gpu_telemetry(self, gpus: list[GpuTelemetryEntry] | None) -> None:
        with self.lock:
            self.gpus = gpus

    def _snapshot(
        self,
    ) -> tuple[int, float | None, bool, bool, list[GpuTelemetryEntry] | None]:
        with self.lock:
            return (
                self.heartbeats_sent,
                self.rtt_ewma_ms,
                self.lease_active,
                self.container_running,
                list(self.gpus) if self.gpus is not None else None,
            )


class _AgentCollector(Collector):
    """Renders :class:`AgentMetricsState` as Prometheus metric families,
    omitting any series NVML has not actually reported this cycle."""

    def __init__(self, state: AgentMetricsState) -> None:
        self._state = state

    def collect(self) -> Iterator[Metric]:
        heartbeats, rtt_ewma_ms, lease_active, container_running, gpus = (
            self._state._snapshot()
        )

        heartbeats_metric = CounterMetricFamily(
            "agent_heartbeats_sent_total",
            "Total heartbeats this agent has successfully sent to the orchestrator.",
        )
        heartbeats_metric.add_metric([], float(heartbeats))
        yield heartbeats_metric

        if rtt_ewma_ms is not None:
            rtt_metric = GaugeMetricFamily(
                "agent_rtt_ewma_ms",
                "Agent-side smoothed round-trip time to the orchestrator (EWMA of "
                "real measured heartbeat round trips).",
            )
            rtt_metric.add_metric([], rtt_ewma_ms)
            yield rtt_metric

        lease_metric = GaugeMetricFamily(
            "agent_lease_active",
            "1 if this node currently holds a lease, else 0.",
        )
        lease_metric.add_metric([], 1.0 if lease_active else 0.0)
        yield lease_metric

        container_metric = GaugeMetricFamily(
            "agent_container_running",
            "1 if this node currently has a trainer container running, else 0.",
        )
        container_metric.add_metric([], 1.0 if container_running else 0.0)
        yield container_metric

        if gpus:
            util = GaugeMetricFamily(
                "agent_gpu_util_percent", "Live GPU utilisation percent (NVML).", labels=["gpu"]
            )
            mem_used = GaugeMetricFamily(
                "agent_gpu_mem_used_bytes", "Live GPU memory used, bytes (NVML).", labels=["gpu"]
            )
            mem_total = GaugeMetricFamily(
                "agent_gpu_mem_total_bytes", "Live GPU memory total, bytes (NVML).", labels=["gpu"]
            )
            temp = GaugeMetricFamily(
                "agent_gpu_temperature_celsius",
                "Live GPU temperature, Celsius (NVML; omitted where the driver "
                "does not expose it).",
                labels=["gpu"],
            )
            power = GaugeMetricFamily(
                "agent_gpu_power_watts",
                "Live GPU power draw, watts (NVML; omitted where the driver does "
                "not expose it).",
                labels=["gpu"],
            )
            any_temp = False
            any_power = False
            for i, gpu in enumerate(gpus):
                gpu_label = str(i)
                util.add_metric([gpu_label], gpu.util_percent)
                mem_used.add_metric([gpu_label], float(gpu.mem_used_bytes))
                mem_total.add_metric([gpu_label], float(gpu.mem_total_bytes))
                if gpu.temperature_c is not None:
                    temp.add_metric([gpu_label], gpu.temperature_c)
                    any_temp = True
                if gpu.power_w is not None:
                    power.add_metric([gpu_label], gpu.power_w)
                    any_power = True
            yield util
            yield mem_used
            yield mem_total
            if any_temp:
                yield temp
            if any_power:
                yield power


def build_registry(state: AgentMetricsState) -> CollectorRegistry:
    """Build a dedicated registry holding only this agent's collector — never
    the process default registry, so nothing else (e.g. prometheus_client's
    own Python GC/process default collectors) leaks in unexpectedly."""
    registry = CollectorRegistry()
    registry.register(_AgentCollector(state))
    return registry


def start_metrics_server(port: int, state: AgentMetricsState) -> None:
    """Start the background HTTP metrics server. Never called unless the
    operator opted in via ``--metrics-port`` (off by default)."""
    start_http_server(port, registry=build_registry(state))
