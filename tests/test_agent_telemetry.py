"""Tests for agent-side telemetry: RTT EWMA math and honest NVML-absent
handling (CONTRIBUTING.md #2, #3 — never invent a number).

``FakeNvmlUnavailable`` stands in for the ``pynvml`` binding itself, so these
tests exercise the exact code path the real agent takes when NVML fails to
initialize (no driver, no GPU, permission denied, ...), without needing GPU
hardware present in CI.
"""

from __future__ import annotations

import pytest

from agent import main as agent_main
from agent.telemetry import nvml as nvml_module
from agent.telemetry.latency import RttEwma, Stopwatch

# --- RTT EWMA -----------------------------------------------------------------


def test_ewma_starts_unseeded() -> None:
    tracker = RttEwma(alpha=0.3)
    assert tracker.value is None


def test_ewma_first_sample_seeds_exactly() -> None:
    """The first observation is not blended with anything — it *is* the
    average, seeded from a single real measurement."""
    tracker = RttEwma(alpha=0.3)
    result = tracker.observe(42.0)
    assert result == 42.0
    assert tracker.value == 42.0


def test_ewma_second_sample_blends_by_alpha() -> None:
    tracker = RttEwma(alpha=0.5)
    tracker.observe(10.0)
    result = tracker.observe(20.0)
    assert result == pytest.approx(15.0)  # 0.5*20 + 0.5*10


def test_ewma_low_alpha_damps_an_outlier() -> None:
    """A low alpha weights history heavily, so one outlier barely moves it."""
    tracker = RttEwma(alpha=0.1)
    tracker.observe(10.0)
    result = tracker.observe(1000.0)
    assert result == pytest.approx(0.1 * 1000.0 + 0.9 * 10.0)
    assert result < 110.0


def test_ewma_high_alpha_tracks_closely() -> None:
    tracker = RttEwma(alpha=0.9)
    tracker.observe(10.0)
    result = tracker.observe(1000.0)
    assert result == pytest.approx(0.9 * 1000.0 + 0.1 * 10.0)
    assert result > 890.0


def test_ewma_rejects_no_fabricated_seed() -> None:
    """Nothing is reported until at least one real measurement exists."""
    tracker = RttEwma(alpha=0.3)
    assert tracker.value is None
    # Only .observe() (fed a real measurement) ever changes .value.


def test_stopwatch_measures_a_real_elapsed_duration() -> None:
    import time

    with Stopwatch() as sw:
        time.sleep(0.05)
    assert sw.elapsed_ms is not None
    assert sw.elapsed_ms >= 40.0  # allow for scheduler slack, never fabricated


# --- NVML-absent handling: no invented numbers, ever --------------------------


class FakeNvmlUnavailable:
    """Stub pynvml binding whose ``nvmlInit`` always fails.

    Simulates the real-world case (no NVIDIA driver, no GPU, permission
    denied) that the agent must handle by reporting absence, never a
    plausible-looking substitute.
    """

    def nvmlInit(self) -> None:  # noqa: N802 - mirrors NVML's actual camelCase API
        raise RuntimeError("NVML: Driver Not Loaded (simulated for test)")


@pytest.fixture
def nvml_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nvml_module, "pynvml", FakeNvmlUnavailable())


def test_gpu_inventory_is_none_when_nvml_unavailable(nvml_unavailable: None) -> None:
    assert nvml_module.collect_gpu_inventory() is None


def test_gpu_telemetry_is_none_when_nvml_unavailable(nvml_unavailable: None) -> None:
    assert nvml_module.collect_gpu_telemetry() is None


def test_agent_hardware_inventory_reports_empty_gpu_list(nvml_unavailable: None) -> None:
    """HardwareInventory.gpus is [] (never a fabricated entry) when NVML is
    unavailable at enrollment time."""
    hardware = agent_main.build_hardware_inventory()
    assert hardware["gpus"] == []
    # Host fields are real, measured values — sanity-checked, not asserted
    # against invented constants.
    assert hardware["cores"] >= 1
    assert hardware["ram_bytes"] > 0


def test_agent_heartbeat_payload_reports_null_gpu(nvml_unavailable: None) -> None:
    """The heartbeat payload's gpu field is null, and no other field in the
    payload is a fabricated number."""
    payload = agent_main.build_heartbeat_payload(rtt_ms=None)
    assert payload["gpu"] is None
    assert payload["rtt_ms"] is None
    # Everything else is a real psutil reading, sanity-bounded rather than
    # pinned to an exact fabricated value.
    assert 0.0 <= payload["cpu_percent"] <= 100.0
    assert 0 <= payload["ram_used_bytes"] <= payload["ram_total_bytes"]


def test_agent_heartbeat_payload_carries_forward_measured_rtt(nvml_unavailable: None) -> None:
    """A previously measured RTT (from the agent's own EWMA) is passed
    through untouched — the agent never invents one."""
    payload = agent_main.build_heartbeat_payload(rtt_ms=12.5)
    assert payload["rtt_ms"] == 12.5
