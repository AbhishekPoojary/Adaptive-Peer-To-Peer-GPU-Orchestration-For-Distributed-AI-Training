"""Real hardware and provenance capture for benchmark artifacts (rule 6).

Every value here is read from the machine at run time. Where something cannot
be determined, it is reported as ``null`` — never as a plausible-looking
placeholder, because a benchmark artifact's whole purpose is that a reader can
trust the numbers in it (CONTRIBUTING.md rules 1-2).
"""

from __future__ import annotations

import contextlib
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

_REPO_ROOT = Path(__file__).resolve().parents[2]


class DirtyWorktreeError(Exception):
    """The working tree has uncommitted changes, so ``git_sha`` would lie."""


def git_sha(*, allow_dirty: bool = False) -> str:
    """Return the exact commit the benchmarked code is running from.

    Refuses a dirty worktree by default. The SHA is the artifact's claim about
    *what was measured*; recording one while uncommitted changes are in the
    tree makes the run unreproducible while looking reproducible. ``--allow-
    dirty`` exists for local iteration and marks the artifact accordingly.
    """
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    if dirty and not allow_dirty:
        raise DirtyWorktreeError(
            "refusing to write a benchmark artifact from a dirty worktree: the "
            "recorded git_sha would not describe the code that ran. Commit "
            "first, or pass --allow-dirty to mark the artifact as provisional."
        )
    return sha


def _gpu_description() -> str | None:
    """Return a real GPU description from NVML, or None if there is no GPU.

    ``None`` is a truthful answer here — a CPU-only machine is a supported
    configuration (ADR-010), not a failure to detect something.
    """
    try:
        import pynvml
    except ImportError:
        return None
    try:
        pynvml.nvmlInit()
    except Exception:  # noqa: BLE001 - no driver / no device / WSL without passthrough
        return None
    try:
        count = pynvml.nvmlDeviceGetCount()
        if count == 0:
            return None
        names: list[str] = []
        for index in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            raw = pynvml.nvmlDeviceGetName(handle)
            name = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            total_gb = pynvml.nvmlDeviceGetMemoryInfo(handle).total / 1024**3
            names.append(f"{name} ({total_gb:.1f} GB)")
        return ", ".join(names)
    except Exception:  # noqa: BLE001
        return None
    finally:
        with contextlib.suppress(Exception):
            pynvml.nvmlShutdown()


def _cpu_description() -> str:
    """Best available real CPU description for this platform."""
    name = platform.processor() or platform.machine() or "unknown"
    physical = psutil.cpu_count(logical=False)
    logical = psutil.cpu_count(logical=True)
    cores = f"{physical}C/{logical}T" if physical else f"{logical}T"
    return f"{name} ({cores})"


@dataclass(frozen=True)
class Hardware:
    """The `hardware` block of a benchmark artifact (bench/schema.json)."""

    cpu: str
    gpu: str | None
    ram_gb: float
    os: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "cpu": self.cpu,
            "gpu": self.gpu,
            "ram_gb": self.ram_gb,
            "os": self.os,
        }


def capture_hardware() -> Hardware:
    """Read this machine's real inventory."""
    return Hardware(
        cpu=_cpu_description(),
        gpu=_gpu_description(),
        ram_gb=round(psutil.virtual_memory().total / 1024**3, 2),
        os=f"{platform.system()} {platform.release()} (python {platform.python_version()})",
    )


@dataclass
class Limitations:
    """What a run did *not* exercise, machine-written into every artifact.

    ADR-013: on a single-host fleet the load and latency terms of
    ``S_i = α·L_i − β·R_i + γ·D_i`` cannot be differentiated between nodes —
    every agent measures the same physical CPU/GPU and dials localhost. Rather
    than leave that in prose a reader might not see, each report carries it
    next to the numbers.

    Computed from the observed fleet, not hardcoded: when real peer laptops
    join, these become true without a harness change.
    """

    load_term_differentiable: bool
    latency_term_differentiable: bool
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "load_term_differentiable": self.load_term_differentiable,
            "latency_term_differentiable": self.latency_term_differentiable,
            "notes": list(self.notes),
        }


def assess_limitations(node_hostnames: list[str]) -> Limitations:
    """Decide which score terms this fleet could actually differentiate.

    The test is whether the nodes are distinct physical machines, which we read
    from the hostnames they reported at enrollment. Co-located agents share a
    CPU and a GPU, so their reported load is the same number by construction;
    they also share a loopback path, so measured RTT differs only by noise.
    """
    distinct_hosts = len(set(node_hostnames))
    single_host = distinct_hosts <= 1
    notes: list[str] = []
    if single_host:
        notes.append(
            f"All {len(node_hostnames)} agents ran on one physical host "
            f"({distinct_hosts} distinct hostname). psutil.cpu_percent() and "
            "NVML utilization are host/device-wide, so every node reports the "
            "same load truthfully; RTT is loopback for all of them. The L and D "
            "terms of S_i are therefore untested here — only the reliability "
            "term R is differentiated (ADR-013)."
        )
    else:
        notes.append(
            f"{distinct_hosts} distinct physical hosts observed across "
            f"{len(node_hostnames)} agents; load and latency are genuinely "
            "per-node."
        )
    return Limitations(
        load_term_differentiable=not single_host,
        latency_term_differentiable=not single_host,
        notes=notes,
    )
