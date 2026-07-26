"""Tests for the agent-side /metrics collector (M7).

Exercises ``_AgentCollector.collect()`` directly (no real HTTP server, no
NVML) against an ``AgentMetricsState`` the test drives itself — this pins the
"absent, never zero" contract for GPU telemetry (CONTRIBUTING.md #2) and the
real-event counters/gauges, without needing GPU hardware in CI.
"""

from __future__ import annotations

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric

from agent.metrics_server import AgentMetricsState, _AgentCollector
from agent.telemetry.nvml import GpuTelemetryEntry


def _collect(state: AgentMetricsState) -> dict[str, Metric]:
    """Collect and index the yielded metric families by name for assertions."""
    families = list(_AgentCollector(state).collect())
    return {f.name: f for f in families}


def test_heartbeats_counter_starts_at_zero_before_any_heartbeat() -> None:
    # CounterMetricFamily strips the trailing "_total" from .name (it lives in
    # the exposition sample name instead) — see prometheus_client.core.
    state = AgentMetricsState()
    families = _collect(state)
    assert isinstance(families["agent_heartbeats_sent"], CounterMetricFamily)
    assert families["agent_heartbeats_sent"].samples[0].name == "agent_heartbeats_sent_total"
    assert families["agent_heartbeats_sent"].samples[0].value == 0.0


def test_heartbeats_counter_reflects_real_recorded_heartbeats() -> None:
    state = AgentMetricsState()
    state.record_heartbeat_sent(rtt_ewma_ms=42.0)
    state.record_heartbeat_sent(rtt_ewma_ms=41.0)
    families = _collect(state)
    assert families["agent_heartbeats_sent"].samples[0].value == 2.0


def test_rtt_gauge_absent_until_a_round_trip_is_measured() -> None:
    """No agent_rtt_ewma_ms series at all before the first real RTT — not a
    fabricated 0."""
    state = AgentMetricsState()
    families = _collect(state)
    assert "agent_rtt_ewma_ms" not in families


def test_rtt_gauge_present_once_measured() -> None:
    state = AgentMetricsState()
    state.record_heartbeat_sent(rtt_ewma_ms=17.5)
    families = _collect(state)
    assert isinstance(families["agent_rtt_ewma_ms"], GaugeMetricFamily)
    assert families["agent_rtt_ewma_ms"].samples[0].value == 17.5


def test_lease_and_container_gauges_reflect_real_state() -> None:
    state = AgentMetricsState()
    families = _collect(state)
    assert families["agent_lease_active"].samples[0].value == 0.0
    assert families["agent_container_running"].samples[0].value == 0.0

    state.record_lease_state(lease_active=True, container_running=True)
    families = _collect(state)
    assert families["agent_lease_active"].samples[0].value == 1.0
    assert families["agent_container_running"].samples[0].value == 1.0


def test_gpu_gauges_absent_when_nvml_has_reported_nothing() -> None:
    """No GPU series at all when NVML is unavailable/absent this cycle —
    never a substituted 0 (CONTRIBUTING.md #2)."""
    state = AgentMetricsState()
    families = _collect(state)
    assert "agent_gpu_util_percent" not in families
    assert "agent_gpu_mem_used_bytes" not in families
    assert "agent_gpu_mem_total_bytes" not in families
    assert "agent_gpu_temperature_celsius" not in families
    assert "agent_gpu_power_watts" not in families


def test_gpu_gauges_present_with_real_values_once_reported() -> None:
    state = AgentMetricsState()
    state.record_gpu_telemetry(
        [
            GpuTelemetryEntry(
                util_percent=55.0,
                mem_used_bytes=2_000_000_000,
                mem_total_bytes=4_000_000_000,
                temperature_c=61.0,
                power_w=None,  # this driver doesn't expose power draw
            )
        ]
    )
    families = _collect(state)
    assert families["agent_gpu_util_percent"].samples[0].value == 55.0
    assert families["agent_gpu_util_percent"].samples[0].labels == {"gpu": "0"}
    assert families["agent_gpu_mem_used_bytes"].samples[0].value == 2_000_000_000.0
    assert families["agent_gpu_temperature_celsius"].samples[0].value == 61.0
    # power_w was None on this reading: its series must not appear at all.
    assert "agent_gpu_power_watts" not in families


def test_multiple_gpus_each_get_their_own_labeled_sample() -> None:
    state = AgentMetricsState()
    state.record_gpu_telemetry(
        [
            GpuTelemetryEntry(
                util_percent=10.0,
                mem_used_bytes=1,
                mem_total_bytes=10,
                temperature_c=50.0,
                power_w=30.0,
            ),
            GpuTelemetryEntry(
                util_percent=90.0,
                mem_used_bytes=9,
                mem_total_bytes=10,
                temperature_c=70.0,
                power_w=90.0,
            ),
        ]
    )
    families = _collect(state)
    labels = {s.labels["gpu"] for s in families["agent_gpu_util_percent"].samples}
    assert labels == {"0", "1"}
