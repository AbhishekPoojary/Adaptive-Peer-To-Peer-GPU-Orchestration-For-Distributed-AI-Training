"""How busy does a peer's GPU actually stay while it trains a job?

The project report targets average GPU utilization above 80% during training.
This runs real jobs on one real agent and samples the device through NVML (the
same library the agent's telemetry uses) twice a second, from the moment the
orchestrator records the job RUNNING until it records it COMPLETED.

Two windows are reported, and neither boundary is chosen by this harness:

* **whole run**: RUNNING to COMPLETED as the orchestrator recorded them, so it
  includes container start, dataset loading and every evaluation, which is
  the run as a person submitting a job experiences it;
* **training phase**: from the trainer's own "dataset ready" log line to its
  "training complete" line, which is every epoch and evaluation and nothing
  before or after. This is the window the report's target describes
  ("during parallel training execution").

Log line times are the orchestrator's receipt times and samples are taken on
this host; both clocks are the same machine's, offset by at most the few
milliseconds the failure_recovery scenario measures.

The GPU is shared with whatever else the host is doing (a desktop compositor,
a browser), so an idle baseline is sampled before each job and reported next
to it. Nothing is subtracted: the baseline is context, not a correction.

Several job configurations are measured in one run (``configs`` in the scenario
JSON), because utilization depends mostly on how much work each step gives the
GPU, and a single number would hide that.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import statistics
import time
from datetime import UTC, datetime
from typing import Any

from agent.telemetry.nvml import collect_gpu_telemetry
from bench.harness.client import BenchClient
from bench.harness.fleet import Fleet

logger = logging.getLogger("bench.gpu_utilization")

NAME = "gpu_utilization"

#: Every result here is a measurement this harness took; none is a verbatim
#: copy of the orchestrator's record, so none may carry a null.
VERBATIM_RESULT_KEYS: frozenset[str] = frozenset()

#: The report's Appendix B target, carried into the artifact as a target.
REPORT_TARGET_PERCENT = 80.0

_SAMPLE_SECONDS = 0.5
_BASELINE_SECONDS = 5.0


def _read_util() -> float:
    gpus = collect_gpu_telemetry()
    if not gpus:
        raise RuntimeError(
            "NVML reports no GPU on this host, so there is no utilization to "
            "measure. This scenario needs the GPU the agent trains on."
        )
    return gpus[0].util_percent


async def _sample_until(stop: asyncio.Event, samples: list[tuple[datetime, float]]) -> None:
    while not stop.is_set():
        value = await asyncio.to_thread(_read_util)
        samples.append((datetime.now(UTC), value))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_SAMPLE_SECONDS)


def _summary(samples: list[float]) -> dict[str, Any]:
    ordered = sorted(samples)
    return {
        "samples": len(samples),
        "mean_percent": round(statistics.fmean(samples), 1),
        "p10_percent": round(ordered[len(ordered) // 10], 1),
        "p50_percent": round(statistics.median(samples), 1),
        "p90_percent": round(ordered[(len(ordered) * 9) // 10], 1),
        "share_at_or_above_target": round(
            sum(1 for s in samples if s >= REPORT_TARGET_PERCENT) / len(samples), 3
        ),
    }


async def _training_phase(client: BenchClient, job_id: str) -> tuple[datetime, datetime]:
    """The trainer's own start and end of training, from its log lines."""
    start = end = None
    for line in await client.logs(job_id):
        text = line["line"]
        if start is None and "dataset ready" in text:
            start = datetime.fromisoformat(line["ts"].replace("Z", "+00:00"))
        if "training complete" in text:
            end = datetime.fromisoformat(line["ts"].replace("Z", "+00:00"))
    if start is None or end is None:
        raise RuntimeError(
            f"job {job_id[:8]}: the trainer's 'dataset ready' / 'training complete' "
            "lines were not both in its log, so the training phase has no boundary"
        )
    return start, end


async def _measure_one(
    client: BenchClient, *, label: str, spec: dict[str, Any], timeout: float
) -> dict[str, Any]:
    baseline: list[float] = []
    deadline = time.monotonic() + _BASELINE_SECONDS
    while time.monotonic() < deadline:
        baseline.append(await asyncio.to_thread(_read_util))
        await asyncio.sleep(_SAMPLE_SECONDS)

    job = await client.submit(spec=spec, scheduler_name="least_loaded")
    job_id = job["id"]
    await client.wait_for_job_state(job_id, states={"RUNNING"}, timeout_seconds=timeout)
    logger.info("%s: job %s running; sampling the GPU", label, job_id[:8])

    samples: list[tuple[datetime, float]] = []
    stop = asyncio.Event()
    sampler = asyncio.create_task(_sample_until(stop, samples))
    started = time.monotonic()
    try:
        completed, _ = await client.wait_for_job_state(
            job_id, states={"COMPLETED", "FAILED"}, timeout_seconds=timeout
        )
    finally:
        stop.set()
        await sampler
    seconds = time.monotonic() - started

    if completed["state"] != "COMPLETED":
        raise RuntimeError(f"{label}: job {job_id[:8]} ended {completed['state']}")
    if len(samples) < 10:  # whole-run samples
        raise RuntimeError(f"{label}: only {len(samples)} samples; the run was too short")
    phase = await _training_phase(client, job_id)
    in_phase = [u for ts, u in samples if phase[0] <= ts <= phase[1]]
    if len(in_phase) < 10:
        raise RuntimeError(f"{label}: only {len(in_phase)} samples inside the training phase")
    utils = [u for _ts, u in samples]
    result = completed.get("result") or {}
    return {
        "label": label,
        "job_id": job_id,
        # Unset fields dropped: the artifact never carries a null.
        "spec": {k: v for k, v in spec.items() if v is not None},
        "running_seconds": round(seconds, 1),
        "final_test_accuracy": result.get("final_test_accuracy"),
        "device": result.get("device"),
        "idle_baseline": _summary(baseline),
        "whole_run": _summary(utils),
        "training_phase_seconds": round((phase[1] - phase[0]).total_seconds(), 1),
        "training_phase": _summary(in_phase),
        "training_phase_meets_report_target": (
            statistics.fmean(in_phase) >= REPORT_TARGET_PERCENT
        ),
    }


async def run(
    *,
    client: BenchClient,
    fleet: Fleet,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Execute the scenario and return its measured results."""
    timeout = float(config.get("job_timeout_seconds", 1800.0))
    await fleet.start_agent("gpu-util")
    await client.wait_for_online_nodes(count=1, timeout_seconds=120.0)

    runs = []
    for entry in config["configs"]:
        runs.append(
            await _measure_one(
                client, label=str(entry["label"]), spec=dict(entry["job_spec"]), timeout=timeout
            )
        )
    return {
        "report_target_percent": REPORT_TARGET_PERCENT,
        "sample_interval_seconds": _SAMPLE_SECONDS,
        "windows": {
            "whole_run": "orchestrator RUNNING -> COMPLETED (container start, data load, "
            "training, evaluation)",
            "training_phase": "trainer log 'dataset ready' -> 'training complete' "
            "(every epoch and evaluation)",
        },
        "runs": runs,
    }
