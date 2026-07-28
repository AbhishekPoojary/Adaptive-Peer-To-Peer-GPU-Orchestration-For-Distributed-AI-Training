"""How long does the system really take to recover from a killed trainer?

Measures the end-to-end fault-tolerance path with a real failure: a running
trainer container is killed with ``docker kill`` — a real SIGKILL to a real
training process — and the harness measures, by wall clock, how long the
orchestrator takes to notice, reassign, and drive the job to a real completion
with a real final accuracy.

Nothing is simulated. There is no failure flag, no injected exception, and no
``time.sleep`` standing in for the recovery (CONTRIBUTING.md rules 4 and 6).

The measurement is deliberately end-to-end rather than a sum of internal
stages: what a user experiences is "my job still finished, and it cost me this
much time", and that is a single number nobody has to trust an internal metric
to believe. The recorded event timeline is included alongside it so the stages
*are* inspectable.

Two agents run so the reassignment has somewhere to go. On one host they are
not independent machines (ADR-013), but recovery latency does not depend on
that: detection is driven by heartbeat silence and lease TTL, both real here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from bench.harness.client import BenchClient
from bench.harness.fleet import Fleet, await_trainer_container, kill_container

logger = logging.getLogger("bench.failure_recovery")

NAME = "failure_recovery"


async def run(
    *,
    client: BenchClient,
    fleet: Fleet,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Execute the scenario and return its measured results."""
    spec = dict(config["job_spec"])
    job_timeout = float(config.get("job_timeout_seconds", 900.0))
    recovery_timeout = float(config.get("recovery_timeout_seconds", 900.0))
    scheduler = str(config.get("scheduler", "adaptive"))

    await fleet.start_agent("recovery-a")
    await fleet.start_agent("recovery-b")
    await client.wait_for_online_nodes(count=2, timeout_seconds=120.0)

    job = await client.submit(spec=spec, scheduler_name=scheduler)
    job_id = job["id"]
    logger.info("submitted job %s under %s", job_id[:8], scheduler)

    container = await await_trainer_container(job_id=job_id, timeout_seconds=job_timeout)
    running = await client.job(job_id)
    original_node = running.get("scheduled_node_id")

    # Let real training make real progress before killing it, so recovery has
    # something to recover *from* rather than restarting a job that had barely
    # begun.
    warmup = float(config.get("warmup_seconds", 20.0))
    logger.info("letting job %s train for %.0fs before the kill", job_id[:8], warmup)
    deadline = time.monotonic() + warmup
    while time.monotonic() < deadline:
        state = (await client.job(job_id))["state"]
        if state in {"COMPLETED", "FAILED", "CANCELLED"}:
            raise RuntimeError(
                f"job {job_id[:8]} reached {state} during warmup — it finished "
                f"before the failure could be induced, so nothing was measured. "
                f"Increase the job's epochs so it outlives the warmup."
            )
        await asyncio.sleep(1.0)

    killed_at = time.monotonic()
    if not kill_container(container):
        raise RuntimeError(
            f"could not kill container {container[:12]}; refusing to report a "
            f"recovery from a failure that never happened"
        )
    logger.info("killed container %s; measuring recovery", container[:12])

    completed, _elapsed = await client.wait_for_job_state(
        job_id, states={"COMPLETED"}, timeout_seconds=recovery_timeout
    )
    recovery_seconds = time.monotonic() - killed_at

    result = completed.get("result") or {}
    final_accuracy = result.get("final_test_accuracy")
    if final_accuracy is None:
        raise RuntimeError(
            f"job {job_id[:8]} completed without a final accuracy; a recovery "
            f"that loses the result is not a recovery"
        )

    final_node = completed.get("scheduled_node_id")
    return {
        "job_id": job_id,
        "scheduler": scheduler,
        "killed_container_id": container[:12],
        "warmup_seconds": warmup,
        # The headline: real wall-clock from SIGKILL to a real completed job.
        "recovery_seconds": round(recovery_seconds, 2),
        "original_node_id": original_node,
        "final_node_id": final_node,
        "reassigned_to_different_node": bool(
            original_node is not None and final_node != original_node
        ),
        "final_lease_epoch": completed["current_lease_epoch"],
        "final_test_accuracy": final_accuracy,
        "final_loss": result.get("final_loss"),
        "epochs_completed": result.get("epochs_completed"),
        "device": result.get("device"),
        # The stage-by-stage record, so the single number above is inspectable.
        "event_timeline": [
            {
                "ts": event["ts"],
                "from_state": event["from_state"],
                "to_state": event["to_state"],
                "detail": event.get("detail", {}),
            }
            for event in completed["events"]
        ],
    }
