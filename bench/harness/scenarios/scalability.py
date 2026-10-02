"""How does the control plane's cost grow as peers join?

The report asks for scalability measured "by increasing the number of
participating GPU nodes and measuring throughput, synchronization delay, and
orchestration overhead". On one laptop with one GPU, more agents cannot train
faster -- they share the device -- so training throughput and synchronization
delay are out of reach here, and the artifact says so. What *is* measurable,
and real, is the orchestration overhead: the fleet is grown one size at a time
(``fleet_sizes``) with genuine agent processes, and at each size this records

* the server-side time to handle a heartbeat and a lease claim, from the
  orchestrator's own request-duration histogram (mean and p95 over a fixed
  window -- deltas between two scrapes, so earlier sizes do not leak in);
* the duration of the scheduler and failure-detector passes, which both walk
  every node;
* heartbeat ingest rate, which is also the telemetry table's growth rate;
* the orchestrator container's CPU and memory, sampled from ``docker stats``;
* real placements: a job is submitted under the adaptive scheduler (the one
  that scores and audits every candidate), timed from submit to SCHEDULED and
  to LEASED from the orchestrator's own event timestamps, then cancelled once
  leased, so that sixteen trainers never fight over one 4 GB GPU. A cancel is
  a clean release and costs the node nothing in reliability.

All agents share this host's CPU with the orchestrator, so every latency here
includes that contention; the artifact records it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import subprocess
import time
from datetime import datetime
from typing import Any

import httpx
from prometheus_client.parser import text_string_to_metric_families

from bench.harness.client import BenchClient
from bench.harness.fleet import Fleet, running_trainer_containers

logger = logging.getLogger("bench.scalability")

NAME = "scalability"

#: Every value is a measurement this harness took.
VERBATIM_RESULT_KEYS: frozenset[str] = frozenset()

_ROUTES = {
    "heartbeat": ("POST", "/nodes/{node_id}/heartbeat"),
    "lease_claim": ("POST", "/nodes/{node_id}/leases/claim"),
    "lease_renew": ("POST", "/leases/{lease_id}/renew"),
}
_PASSES = {
    "scheduler_pass": "orchestrator_scheduler_pass_seconds",
    "failure_detector_pass": "orchestrator_failure_detector_pass_seconds",
}


def _histograms(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]]:
    """Every histogram series as {(name, labels): {count, sum, buckets}}."""
    out: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
    for family in text_string_to_metric_families(text):
        if family.type != "histogram":
            continue
        for sample in family.samples:
            labels = {k: v for k, v in sample.labels.items() if k != "le"}
            key = (family.name, tuple(sorted(labels.items())))
            entry = out.setdefault(key, {"count": 0.0, "sum": 0.0, "buckets": {}})
            if sample.name.endswith("_count"):
                entry["count"] = sample.value
            elif sample.name.endswith("_sum"):
                entry["sum"] = sample.value
            elif sample.name.endswith("_bucket"):
                entry["buckets"][float(sample.labels["le"])] = sample.value
    return out


def _window(before: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, Any] | None:
    """Mean and p95 (as a bucket upper bound) of observations between scrapes."""
    base = before or {"count": 0.0, "sum": 0.0, "buckets": {}}
    count = after["count"] - base["count"]
    if count <= 0:
        return None
    total = after["sum"] - base["sum"]
    p95 = None
    for bound in sorted(after["buckets"]):
        delta = after["buckets"][bound] - base["buckets"].get(bound, 0.0)
        if delta >= 0.95 * count:
            p95 = bound
            break
    return {
        "observations": int(count),
        "mean_ms": round(1000 * total / count, 2),
        # Histogram buckets give an upper bound, not an exact percentile.
        "p95_at_most_ms": round(1000 * p95, 1) if p95 is not None and p95 != float("inf") else
        "above the largest bucket",
    }


async def _scrape(base_url: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]]:
    async with httpx.AsyncClient(timeout=10.0) as http:
        response = await http.get(f"{base_url}/metrics")
        response.raise_for_status()
        return _histograms(response.text)


def _docker_stats(container: str) -> tuple[float, float] | None:
    """(CPU %, memory MiB) of a container right now, or None if unavailable."""
    result = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", container],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    stats = json.loads(result.stdout.strip().splitlines()[0])
    cpu = float(stats["CPUPerc"].rstrip("%"))
    used = stats["MemUsage"].split("/")[0].strip()
    number, unit = float(used[:-3]), used[-3:]
    mib = number * {"KiB": 1 / 1024, "MiB": 1.0, "GiB": 1024.0}.get(unit, 1.0)
    return cpu, mib


def _ts(event: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(event["ts"].replace("Z", "+00:00"))


async def _placement(client: BenchClient, spec: dict[str, Any]) -> dict[str, float]:
    """Submit, wait until leased, cancel; return the orchestrator's own timings."""
    job = await client.submit(spec=spec, scheduler_name="adaptive")
    job_id = job["id"]
    leased, _ = await client.wait_for_job_state(
        job_id, states={"LEASED", "RUNNING"}, timeout_seconds=60.0
    )
    await client.cancel(job_id)
    await client.wait_for_job_state(job_id, states={"CANCELLED"}, timeout_seconds=60.0)
    # The agent learns of the cancel at its next renewal and only then stops
    # the trainer and becomes free. Waiting for the container to go keeps the
    # next placement from measuring that wait instead of the scheduler.
    deadline = time.monotonic() + 60.0
    while running_trainer_containers(job_id=job_id):
        if time.monotonic() > deadline:
            raise RuntimeError(f"trainer for cancelled job {job_id[:8]} never stopped")
        await asyncio.sleep(0.5)
    events = leased["events"]
    queued = next(e for e in events if e["to_state"] == "QUEUED")
    scheduled = next(e for e in events if e["to_state"] == "SCHEDULED")
    lease = next(e for e in events if e["to_state"] == "LEASED")
    return {
        "submit_to_scheduled_ms": (_ts(scheduled) - _ts(queued)).total_seconds() * 1000,
        "scheduled_to_leased_ms": (_ts(lease) - _ts(scheduled)).total_seconds() * 1000,
    }


async def run(*, client: BenchClient, fleet: Fleet, config: dict[str, Any]) -> dict[str, Any]:
    """Execute the scenario and return its measured results."""
    sizes = [int(n) for n in config["fleet_sizes"]]
    window = float(config.get("window_seconds", 30.0))
    settle = float(config.get("settle_seconds", 10.0))
    placements = int(config.get("placements_per_size", 10))
    container = str(config.get("orchestrator_container", "deploy-orchestrator-1"))
    spec = dict(config["job_spec"])
    base_url = client.base_url

    results = []
    for size in sizes:
        while len(fleet.agents) < size:
            await fleet.start_agent(f"scale-{len(fleet.agents) + 1:02d}")
        await client.wait_for_online_nodes(count=size, timeout_seconds=180.0)
        logger.info("fleet at %d agents; settling %.0fs", size, settle)
        await asyncio.sleep(settle)

        before = await _scrape(base_url)
        resources = []
        deadline = time.monotonic() + window
        while time.monotonic() < deadline:
            sample = await asyncio.to_thread(_docker_stats, container)
            if sample is not None:
                resources.append(sample)
            await asyncio.sleep(2.0)
        after = await _scrape(base_url)

        requests: dict[str, Any] = {}
        for name, (method, route) in _ROUTES.items():
            key = ("orchestrator_http_request_seconds",
                   tuple(sorted({"method": method, "route": route}.items())))
            if key in after:
                requests[name] = _window(before.get(key), after[key])
        passes: dict[str, Any] = {}
        for name, metric in _PASSES.items():
            key = (metric, ())
            if key in after:
                passes[name] = _window(before.get(key), after[key])
        heartbeats = (requests.get("heartbeat") or {}).get("observations", 0)

        timings = [await _placement(client, spec) for _ in range(placements)]
        to_scheduled = [t["submit_to_scheduled_ms"] for t in timings]
        to_leased = [t["scheduled_to_leased_ms"] for t in timings]

        entry: dict[str, Any] = {
            "agents": size,
            "window_seconds": window,
            "heartbeats_per_second": round(heartbeats / window, 2),
            "request_handling": {k: v for k, v in requests.items() if v is not None},
            "background_passes": {k: v for k, v in passes.items() if v is not None},
            "placements": placements,
            "submit_to_scheduled_ms": {
                "median": round(statistics.median(to_scheduled), 1),
                "max": round(max(to_scheduled), 1),
            },
            "scheduled_to_leased_ms": {
                "median": round(statistics.median(to_leased), 1),
                "max": round(max(to_leased), 1),
            },
        }
        if resources:
            entry["orchestrator_cpu_percent_mean"] = round(
                statistics.fmean(c for c, _m in resources), 1
            )
            entry["orchestrator_memory_mib_max"] = round(max(m for _c, m in resources), 1)
        results.append(entry)
        logger.info(
            "%d agents: heartbeat %s ms, scheduled in %.0f ms (median)",
            size,
            (requests.get("heartbeat") or {}).get("mean_ms"),
            entry["submit_to_scheduled_ms"]["median"],
        )

    return {
        "fleet_sizes": sizes,
        "by_fleet_size": results,
        "not_measured_here": [
            "training throughput vs. node count: every agent shares one GPU",
            "gradient synchronization delay: needs ranks on separate machines",
        ],
        "contention_note": "all agents and the orchestrator share one host's CPU",
    }
