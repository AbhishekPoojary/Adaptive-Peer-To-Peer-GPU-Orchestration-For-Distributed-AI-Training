"""Does the adaptive scheduler prefer the closer of two otherwise-equal nodes?

Every earlier benchmark ran agents on one laptop dialling loopback, so the
``γ·D_i`` latency term of ``S_i = α·L_i − β·R_i + γ·D_i`` was never tested
(ADR-013): each node's RTT differed only by noise. Here it is, with real delay.

Two agents run in identical Linux containers from one image on one VM, so
their load is the same by construction and their reliability starts equal.
The only difference is that one has ``tc netem`` adding real delay to every
packet it sends. Nothing is simulated in the orchestrator or the agent: the
far agent's heartbeats genuinely take longer, it genuinely measures that, and
the scheduler reads the RTT the agent reported.

The hypothesis mirrors reliability_placement's: given equal load and equal
history, only the adaptive scheduler has an input that separates the two, so
only it should consistently pick the near node. least_loaded and round_robin
are run the same way as controls.

The agents use the agent's test-only ``--release-after`` hold path, so they
never train (no Docker or GPU inside the containers); each job is cancelled as
soon as it is placed, which releases the lease without counting against
either node's reliability.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from bench.harness.client import BenchClient
from bench.harness.fleet import Fleet
from bench.harness.inventory import Limitations

logger = logging.getLogger("bench.latency_placement")

NAME = "latency_placement"

#: The scheduling-decision audit rows are copied verbatim from the orchestrator.
VERBATIM_RESULT_KEYS = frozenset({"first_adaptive_decision"})

_IMAGE = "gpu-orchestrator-agent-netem"
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _start(label: str, *, token: str, orchestrator: str, delay_ms: int) -> str:
    """Start one containerised agent, with ``delay_ms`` of netem egress delay."""
    delay = (
        f"tc qdisc add dev eth0 root netem delay {delay_ms}ms && " if delay_ms else ""
    )
    command = (
        f"{delay}exec python -m agent --orchestrator {orchestrator} "
        f"--enrollment-token {token} --state-dir /state --release-after 3600 "
        "--update-check-minutes 0 --trainer-image-refresh-hours 0"
    )
    _docker("rm", "-f", label, check=False)
    return _docker(
        "run", "-d", "--name", label, "--hostname", label, "--cap-add", "NET_ADMIN",
        _IMAGE, command,
    )


async def _await_node(client: BenchClient, hostname: str, timeout: float = 120.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for node in await client.nodes():
            hardware = node.get("hardware") or {}
            if hardware.get("hostname") == hostname and node["status"] == "ONLINE":
                return node
        await asyncio.sleep(1.0)
    logs = _docker("logs", "--tail", "30", hostname, check=False)
    raise TimeoutError(f"agent {hostname} never came ONLINE. Container log:\n{logs}")


async def _rtt(client: BenchClient, node_id: str) -> float | None:
    for node in await client.nodes():
        if node["id"] == node_id:
            telemetry = node.get("latest_telemetry") or {}
            value = telemetry.get("rtt_ewma_ms")
            return float(value) if value is not None else None
    return None


async def run(*, client: BenchClient, fleet: Fleet, config: dict[str, Any]) -> dict[str, Any]:
    """Execute the scenario and return its measured results."""
    delay_ms = int(config["delay_ms"])
    trials = int(config.get("trials_per_scheduler", 10))
    schedulers = list(config.get("schedulers", ["adaptive", "least_loaded", "round_robin"]))
    settle = float(config.get("settle_seconds", 20.0))
    spec = dict(config["job_spec"])
    # From inside a container the host's published port is reached by this name.
    orchestrator = client.base_url.replace("localhost", "host.docker.internal").replace(
        "127.0.0.1", "host.docker.internal"
    )

    logger.info("building %s", _IMAGE)
    _docker("build", "-q", "-t", _IMAGE, "-f", "bench/agent-netem/Dockerfile", str(_REPO_ROOT))

    names = {"near": "bench-agent-near", "far": "bench-agent-far"}
    try:
        for role, name in names.items():
            token = await client.mint_enrollment_token(created_by=f"bench-{name}")
            _start(name, token=token, orchestrator=orchestrator,
                   delay_ms=delay_ms if role == "far" else 0)
        nodes = {role: await _await_node(client, name) for role, name in names.items()}
        logger.info("both agents ONLINE; letting RTT settle %.0fs", settle)
        await asyncio.sleep(settle)

        rtts = {role: await _rtt(client, str(node["id"])) for role, node in nodes.items()}
        if rtts["near"] is None or rtts["far"] is None:
            raise RuntimeError(f"an agent reported no RTT after settling: {rtts}")
        if rtts["far"] - rtts["near"] < delay_ms / 2:
            raise RuntimeError(
                f"the injected {delay_ms} ms did not reach the scheduler's input "
                f"(RTT EWMA near={rtts['near']:.1f} ms, far={rtts['far']:.1f} ms)"
            )

        near_id = str(nodes["near"]["id"])
        far_id = str(nodes["far"]["id"])
        by_scheduler: dict[str, Any] = {}
        first_decision: list[dict[str, Any]] = []
        for scheduler in schedulers:
            on_near = 0
            on_far = 0
            for _ in range(trials):
                job = await client.submit(spec=spec, scheduler_name=scheduler)
                placed, _ = await client.wait_for_job_state(
                    job["id"], states={"SCHEDULED", "LEASED", "RUNNING"}, timeout_seconds=60.0
                )
                chosen = placed.get("scheduled_node_id")
                on_near += chosen == near_id
                on_far += chosen == far_id
                if scheduler == "adaptive" and not first_decision:
                    first_decision = await client.scheduling_decisions(job["id"])
                await client.cancel(job["id"])
                await client.wait_for_job_state(
                    job["id"], states={"CANCELLED"}, timeout_seconds=60.0
                )
            by_scheduler[scheduler] = {
                "trials": trials,
                "placed_on_near": on_near,
                "placed_on_far": on_far,
                "near_share": round(on_near / trials, 3),
            }
            logger.info("%s: %d/%d on the near node", scheduler, on_near, trials)

        return {
            "hypothesis": (
                "With equal load and equal history, only the adaptive scheduler has an "
                "input (measured RTT) that separates the two nodes, so only it should "
                "consistently prefer the near one."
            ),
            "injected_delay_ms": delay_ms,
            "delay_mechanism": "tc netem on the far agent container's egress (eth0)",
            "rtt_ewma_ms_after_settling": {k: round(v, 1) for k, v in rtts.items() if v},
            "nodes": {
                "near": {"node_id": near_id, "node_name": nodes["near"]["name"]},
                "far": {"node_id": far_id, "node_name": nodes["far"]["name"]},
            },
            "placement_by_scheduler": by_scheduler,
            "first_adaptive_decision": first_decision,
        }
    finally:
        for name in names.values():
            _docker("rm", "-f", name, check=False)


def limitations(_fleet: Fleet) -> Limitations:
    """Declared, not inferred from hostnames: the two containers have different
    hostnames but share one VM, so their load is identical by construction."""
    return Limitations(
        load_term_differentiable=False,
        latency_term_differentiable=True,
        notes=[
            "Both agents ran in identical containers on one Docker VM, so the "
            "load term L could not differ between them; only latency was varied.",
            "Latency was real: tc netem delayed every packet the far agent sent. "
            "The delay is synthetic in origin (one fixed value, no jitter or "
            "loss), not a measurement of a real long-distance link.",
        ],
    )
