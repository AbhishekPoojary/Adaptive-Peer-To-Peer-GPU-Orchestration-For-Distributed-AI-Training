"""Does the adaptive scheduler actually route around unreliable nodes?

This is the project's headline claim under test (ADR-009, ADR-013).

The experiment
--------------
Two real agents, one physical host. On this hardware their load and latency are
identical by construction — same CPU, same GPU, same loopback — so the only
term of ``S_i = α·L_i − β·R_i + γ·D_i`` that can differ is reliability. That
makes for a sharp test rather than a weak one, because reliability is precisely
where ``adaptive`` must part company with the baselines:

    Two nodes, equally loaded and equally close. ``least_loaded`` cannot tell
    them apart. ``round_robin`` will not try. Only ``adaptive`` has an input
    that distinguishes them — the recorded lease history. So an *idle but
    unreliable* node is the discriminating case.

Phase 1 builds that history for real. Each node runs **alone** while its own
history accrues, so there is no ambiguity about which node received which
outcome:

* the *degraded* node gets real failures — its trainer container is killed with
  ``docker kill``, a real SIGKILL to a real training process, which the agent
  observes as a real nonzero exit and reports through the real ``/fail`` path;
* the *healthy* node gets real successes — real MNIST training runs to real
  completion with a real measured accuracy.

Phase 2 brings both online and submits jobs under each scheduler, recording
where each landed. Trials are cancelled rather than trained, because placement
is what is being measured and cancelling releases the lease as ``RELEASED``,
which ``services.jobs.cancel_job`` deliberately does not count against
reliability — so the trials cannot disturb the very state under test.

What a reader may conclude: whether adaptive avoided the node that really
failed. What they may not: anything about throughput, ``α``, or ``γ``. The
artifact's ``limitations`` block says so in machine-readable form.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from bench.harness.client import BenchClient
from bench.harness.fleet import (
    AgentProcess,
    Fleet,
    await_trainer_container,
    kill_container,
)

logger = logging.getLogger("bench.reliability_placement")

NAME = "reliability_placement"

#: Terminal job states, used when waiting for a trial to settle.
_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


async def _node_counters(client: BenchClient, node_id: str) -> tuple[int, int]:
    """This node's real recorded (successes, failures)."""
    for node in await client.nodes():
        if node["id"] == node_id:
            return int(node["lease_success_count"]), int(node["lease_failure_count"])
    raise RuntimeError(f"node {node_id} vanished from the fleet mid-run")


async def _await_counter_change(
    client: BenchClient,
    node_id: str,
    *,
    baseline: tuple[int, int],
    timeout_seconds: float,
) -> tuple[int, int]:
    """Wait until the node's recorded counters actually move.

    The benchmark waits on the *recorded outcome*, not on a sleep: the point is
    that the orchestrator really registered the failure or success, and a timer
    would only assume it did.
    """
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        current = await _node_counters(client, node_id)
        if current != baseline:
            return current
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(
                f"node counters stayed at {baseline} for {timeout_seconds:.0f}s; "
                f"the outcome was never recorded"
            )
        await asyncio.sleep(1.0)


async def _induce_failure(
    client: BenchClient, agent: AgentProcess, *, spec: dict[str, Any], timeout: float
) -> dict[str, Any]:
    """Run one job on this node and kill its trainer. Returns what was measured.

    A real container receives a real SIGKILL. Nothing here simulates a failure
    flag (CONTRIBUTING.md rule 6).
    """
    assert agent.node_id is not None
    baseline = await _node_counters(client, agent.node_id)

    job = await client.submit(spec=spec, scheduler_name="least_loaded")
    job_id = job["id"]
    container = await await_trainer_container(job_id=job_id, timeout_seconds=timeout)
    killed = kill_container(container)
    if not killed:
        raise RuntimeError(
            f"could not kill container {container[:12]} for job {job_id[:8]}; "
            f"refusing to record a failure that was not actually induced"
        )

    after = await _await_counter_change(
        client, agent.node_id, baseline=baseline, timeout_seconds=timeout
    )
    # The job will be reassigned by the real fault-tolerance path; cancel it so
    # it does not keep retrying onto this node and skew later measurements.
    await _cancel_quietly(client, job_id)
    return {
        "job_id": job_id,
        "container_id": container[:12],
        "counters_before": {"ok": baseline[0], "fail": baseline[1]},
        "counters_after": {"ok": after[0], "fail": after[1]},
    }


async def _induce_success(
    client: BenchClient, agent: AgentProcess, *, spec: dict[str, Any], timeout: float
) -> dict[str, Any]:
    """Run one job on this node to real completion. Returns what was measured."""
    assert agent.node_id is not None
    baseline = await _node_counters(client, agent.node_id)

    job = await client.submit(spec=spec, scheduler_name="least_loaded")
    job_id = job["id"]
    completed, elapsed = await client.wait_for_job_state(
        job_id, states={"COMPLETED"}, timeout_seconds=timeout
    )
    after = await _node_counters(client, agent.node_id)
    result = completed.get("result") or {}
    return {
        "job_id": job_id,
        "seconds": round(elapsed, 2),
        "final_test_accuracy": result.get("final_test_accuracy"),
        "device": result.get("device"),
        "counters_before": {"ok": baseline[0], "fail": baseline[1]},
        "counters_after": {"ok": after[0], "fail": after[1]},
    }


async def _cancel_quietly(client: BenchClient, job_id: str) -> None:
    """Cancel a job, tolerating it having already reached a terminal state."""
    try:
        await client.cancel(job_id)
    except Exception as exc:  # noqa: BLE001 - a 409 here just means it finished first
        logger.debug("cancel of %s was a no-op: %s", job_id[:8], exc)


async def _placement_trial(
    client: BenchClient, *, spec: dict[str, Any], scheduler: str, timeout: float
) -> dict[str, Any]:
    """Submit one job, record where the scheduler put it, then cancel.

    Returns the placement plus, for adaptive, the orchestrator's own recorded
    score breakdown — so the artifact carries the scheduler's reasoning, not
    just its verdict.
    """
    job = await client.submit(spec=spec, scheduler_name=scheduler)
    job_id = job["id"]
    placed, _elapsed = await client.wait_for_job_state(
        job_id,
        states={"SCHEDULED", "LEASED", "RUNNING", *_TERMINAL},
        timeout_seconds=timeout,
    )
    node_id = placed.get("scheduled_node_id")
    decisions = await client.scheduling_decisions(job_id)
    await _cancel_quietly(client, job_id)

    if node_id is None:
        raise RuntimeError(
            f"job {job_id[:8]} reached {placed['state']} with no scheduled node; "
            f"a placement trial with no placement is not a measurement"
        )
    return {
        "job_id": job_id,
        "scheduled_node_id": node_id,
        "decisions": decisions,
    }


async def run(
    *,
    client: BenchClient,
    fleet: Fleet,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Execute the scenario and return its measured results."""
    spec = dict(config["job_spec"])
    failures = int(config["degraded_failures"])
    successes = int(config["healthy_successes"])
    trials = int(config["placement_trials"])
    schedulers = list(config["schedulers"])
    job_timeout = float(config.get("job_timeout_seconds", 600.0))
    trial_timeout = float(config.get("trial_timeout_seconds", 120.0))

    # --- Phase 1: build real, divergent lease history ------------------------
    # Each node runs alone so every recorded outcome is unambiguously its own.
    logger.info("phase 1a: %d real failures on the degraded node", failures)
    degraded = await fleet.start_agent("degraded")
    induced_failures = [
        await _induce_failure(client, degraded, spec=spec, timeout=job_timeout)
        for _ in range(failures)
    ]
    await fleet.stop_agent(degraded, await_offline=True)

    logger.info("phase 1b: %d real completions on the healthy node", successes)
    healthy = await fleet.start_agent("healthy")
    induced_successes = [
        await _induce_success(client, healthy, spec=spec, timeout=job_timeout)
        for _ in range(successes)
    ]

    # --- Phase 2: both online, measure placement -----------------------------
    await fleet.restart_agent(degraded)
    await client.wait_for_online_nodes(count=2, timeout_seconds=90.0)
    logger.info("phase 2: %d placement trials per scheduler", trials)

    assert degraded.node_id is not None and healthy.node_id is not None
    degraded_ok, degraded_fail = await _node_counters(client, degraded.node_id)
    healthy_ok, healthy_fail = await _node_counters(client, healthy.node_id)

    if (degraded_fail, healthy_ok) == (0, 0):
        raise RuntimeError(
            "phase 1 produced no divergent history — there is nothing for the "
            "schedulers to distinguish, so phase 2 would measure noise"
        )

    per_scheduler: dict[str, Any] = {}
    for scheduler in schedulers:
        placements: list[dict[str, Any]] = []
        for _ in range(trials):
            placements.append(
                await _placement_trial(
                    client, spec=spec, scheduler=scheduler, timeout=trial_timeout
                )
            )
        to_healthy = sum(
            1 for p in placements if p["scheduled_node_id"] == healthy.node_id
        )
        to_degraded = sum(
            1 for p in placements if p["scheduled_node_id"] == degraded.node_id
        )
        per_scheduler[scheduler] = {
            "trials": len(placements),
            "placed_on_healthy": to_healthy,
            "placed_on_degraded": to_degraded,
            "healthy_share": round(to_healthy / len(placements), 4),
            "placements": placements,
        }
        logger.info(
            "%s: %d/%d placements on the healthy node",
            scheduler,
            to_healthy,
            len(placements),
        )

    return {
        "hypothesis": (
            "Given two nodes that are equally loaded and equally close, only "
            "the adaptive scheduler has an input (recorded lease history) that "
            "distinguishes them, so only it should prefer the reliable node."
        ),
        "nodes": {
            "healthy": {
                "node_id": healthy.node_id,
                "node_name": healthy.node_name,
                "lease_success_count": healthy_ok,
                "lease_failure_count": healthy_fail,
            },
            "degraded": {
                "node_id": degraded.node_id,
                "node_name": degraded.node_name,
                "lease_success_count": degraded_ok,
                "lease_failure_count": degraded_fail,
            },
        },
        "history_construction": {
            "induced_failures": induced_failures,
            "induced_successes": induced_successes,
        },
        "placement_by_scheduler": per_scheduler,
    }
