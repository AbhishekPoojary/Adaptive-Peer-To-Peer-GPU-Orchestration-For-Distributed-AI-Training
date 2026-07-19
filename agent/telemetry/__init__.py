"""Hardware telemetry collection (NVML for GPU, psutil for CPU/host).

Law (see CONTRIBUTING.md #2): every value reported here is either read
from real hardware/OS interfaces, or reported as `null`. Nothing is
substituted with a plausible-looking number when a read fails or the
hardware is absent (e.g. CPU-only nodes report `gpu: null`, not a fake
GPU record).
"""
