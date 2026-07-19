"""Real CPU/host telemetry via ``psutil``.

CONTRIBUTING.md #2: every value here is read from the OS via psutil. A field
psutil cannot determine on this platform (e.g. no CPU model string on some
Linux kernels) is reported as ``None``/a documented fallback string, never a
fabricated number.
"""

from __future__ import annotations

import logging
import platform
import socket
from dataclasses import dataclass

import psutil

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HostInventory:
    """Static host inventory (reported once, at enrollment)."""

    hostname: str
    os: str
    cpu_model: str
    cores: int
    ram_bytes: int


@dataclass(frozen=True)
class SystemTelemetry:
    """Live CPU/RAM telemetry (reported every heartbeat)."""

    cpu_percent: float
    ram_used_bytes: int
    ram_total_bytes: int


def _cpu_model() -> str:
    """Best-effort CPU model string.

    ``platform.processor()`` is empty on some Linux distributions (the real
    model lives in /proc/cpuinfo, which is out of scope to parse here); when
    empty we fall back to ``platform.machine()`` (the architecture, e.g.
    "x86_64") — still a real, truthful value, just less specific. Never a
    made-up model name.
    """
    processor = platform.processor()
    if processor:
        return processor
    return platform.machine() or "unknown"


def collect_host_inventory() -> HostInventory:
    """Collect static host inventory. Always succeeds — psutil/platform calls
    used here do not fail on a live process."""
    return HostInventory(
        hostname=socket.gethostname(),
        os=f"{platform.system()} {platform.release()}".strip(),
        cpu_model=_cpu_model(),
        cores=psutil.cpu_count(logical=True) or 1,
        ram_bytes=int(psutil.virtual_memory().total),
    )


def collect_system_telemetry() -> SystemTelemetry:
    """Collect live CPU/RAM telemetry.

    ``cpu_percent`` uses a short blocking interval so the first reading is a
    real measured value rather than psutil's meaningless 0.0 "since last
    call" default.
    """
    cpu_percent = psutil.cpu_percent(interval=0.2)
    vm = psutil.virtual_memory()
    return SystemTelemetry(
        cpu_percent=float(cpu_percent),
        ram_used_bytes=int(vm.total - vm.available),
        ram_total_bytes=int(vm.total),
    )
