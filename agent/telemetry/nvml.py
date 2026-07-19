"""Real GPU telemetry via NVML (nvidia-ml-py's ``pynvml`` bindings).

CONTRIBUTING.md #2: every value here is read from NVML, or is ``None``. If
NVML fails to initialize (no driver, no GPU, permission denied, ...) or
reports zero devices, :func:`collect_gpu_inventory` and
:func:`collect_gpu_telemetry` both return ``None`` — the caller then reports
``gpus: []`` in hardware inventory and ``gpu: null`` in heartbeat telemetry.
Nothing is ever substituted with a plausible-looking number.

Per-GPU, per-field handling: a handful of NVML calls (notably power draw on
some laptop GPUs, and some ECC/clock queries) can raise
``NVML_ERROR_NOT_SUPPORTED`` on a specific device while every other call
succeeds. Each such field is caught individually and reported as ``None``;
one unsupported field never blanks out the rest of that GPU's real readings.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

try:
    import pynvml

    _NVML_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - exercised only without the dep
    pynvml = None
    _NVML_IMPORT_ERROR = exc


@dataclass(frozen=True)
class GpuInventoryEntry:
    """Static per-GPU inventory (reported once, at enrollment)."""

    name: str
    vram_bytes: int
    driver_version: str


@dataclass(frozen=True)
class GpuTelemetryEntry:
    """Live per-GPU telemetry (reported every heartbeat)."""

    util_percent: float
    mem_used_bytes: int
    mem_total_bytes: int
    temperature_c: float | None
    power_w: float | None


def _nvml_available() -> bool:
    if pynvml is None:
        logger.info("NVML unavailable: pynvml is not installed (%s)", _NVML_IMPORT_ERROR)
        return False
    try:
        pynvml.nvmlInit()
    except Exception as exc:  # NVMLError or anything else the binding raises
        logger.info("NVML unavailable: nvmlInit failed (%s)", exc)
        return False
    return True


def _shutdown() -> None:
    # Best-effort: nothing to report if shutdown itself fails.
    with contextlib.suppress(Exception):
        pynvml.nvmlShutdown()


def _device_count() -> int | None:
    try:
        return int(pynvml.nvmlDeviceGetCount())
    except Exception as exc:
        logger.info("NVML unavailable: nvmlDeviceGetCount failed (%s)", exc)
        return None


def _decode(value: bytes | str) -> str:
    """NVML string getters return ``bytes`` on some bindings, ``str`` on others."""
    return value.decode("utf-8") if isinstance(value, bytes) else value


def collect_gpu_inventory() -> list[GpuInventoryEntry] | None:
    """Return static per-GPU inventory, or None if NVML/GPUs are unavailable.

    None (not []) signals "could not determine" so callers can distinguish
    "NVML unavailable" from "genuinely zero GPUs" if that distinction ever
    matters; the HardwareInventory schema itself just wants a list, so
    agent.main maps None -> [].
    """
    if not _nvml_available():
        return None
    try:
        count = _device_count()
        if count is None:
            return None
        driver_version = _decode(pynvml.nvmlSystemGetDriverVersion())
        entries: list[GpuInventoryEntry] = []
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = _decode(pynvml.nvmlDeviceGetName(handle))
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            entries.append(
                GpuInventoryEntry(
                    name=name, vram_bytes=int(mem.total), driver_version=driver_version
                )
            )
        return entries
    except Exception as exc:
        logger.info("NVML inventory collection failed (%s)", exc)
        return None
    finally:
        _shutdown()


def _optional_temperature(handle: object) -> float | None:
    try:
        return float(
            pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
        )
    except Exception as exc:
        logger.debug("NVML temperature read unavailable for this GPU (%s)", exc)
        return None


def _optional_power_w(handle: object) -> float | None:
    try:
        milliwatts = pynvml.nvmlDeviceGetPowerUsage(handle)
        return float(milliwatts) / 1000.0
    except Exception as exc:
        logger.debug("NVML power draw unavailable for this GPU (%s)", exc)
        return None


def collect_gpu_telemetry() -> list[GpuTelemetryEntry] | None:
    """Return live per-GPU telemetry, or None if NVML/GPUs are unavailable.

    ``temperature_c``/``power_w`` are read independently per GPU: if NVML
    can't provide one of them on this device it is None while util/mem (which
    NVML universally supports) still carry real readings.
    """
    if not _nvml_available():
        return None
    try:
        count = _device_count()
        if count is None:
            return None
        entries: list[GpuTelemetryEntry] = []
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            entries.append(
                GpuTelemetryEntry(
                    util_percent=float(util.gpu),
                    mem_used_bytes=int(mem.used),
                    mem_total_bytes=int(mem.total),
                    temperature_c=_optional_temperature(handle),
                    power_w=_optional_power_w(handle),
                )
            )
        return entries
    except Exception as exc:
        logger.info("NVML telemetry collection failed (%s)", exc)
        return None
    finally:
        _shutdown()
