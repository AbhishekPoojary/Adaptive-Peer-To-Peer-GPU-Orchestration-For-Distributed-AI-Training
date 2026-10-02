"""How long does the system really take to recover when a peer disappears?

Measures the end-to-end fault-tolerance path with a real failure: the agent
process holding a running job is SIGKILLed, with no chance to release its lease
or say anything to the orchestrator. Recovery must therefore come from the
orchestrator noticing silence on its own — φ-accrual detection (ADR-004), lease
expiry, reassignment to another peer, and a real completion with a real final
accuracy.

Nothing is simulated. No failure flag, no injected exception, no ``time.sleep``
standing in for the recovery (CONTRIBUTING.md rules 4 and 6).

Why the agent and not the trainer container
-------------------------------------------
An earlier version of this scenario killed the trainer container instead. That
measured nothing, and the first run said so: the job went straight to ``FAILED``
at lease epoch 1 with ``trainer exited with code 137``, never reassigned.

That is correct behaviour, not a bug. The system deliberately separates two
cases (``services/leases.py``, ADR-005):

* a **reported** training failure — the agent watched the trainer exit nonzero
  and said so — is **terminal**. Retrying a job whose spec is broken would burn
  the whole fleet in a loop, so failures surface instead of being masked.
* a **dropped node** — heartbeat silence, lease expires — is the retryable
  case, and goes ``REASSIGNED``.

Killing the container exercises the first path, which is designed not to
recover. Killing the agent exercises the second, which is the fault tolerance
this project actually claims. The scenario was wrong; the system was right.

A known tension, recorded rather than resolved here: exit code 137 is
``SIGKILL``, which on a 4 GB laptop GPU is quite often the OOM killer — a
*node-specific* failure that a larger peer might survive. The current design
cannot distinguish that from a genuinely broken job spec, so it treats both as
terminal. Bounded retry with failure classification would be a real improvement
and a real design change; it is out of scope for M9, which measures what exists.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from bench.harness.client import BenchClient
from bench.harness.fleet import Fleet, await_trainer_container, kill_container

logger = logging.getLogger("bench.failure_recovery")

NAME = "failure_recovery"

#: Result keys holding a faithful copy of the orchestrator's own record rather
#: than a measurement this harness took. Nulls inside them are data — a job's
#: first state transition really does have ``from_state: null`` — so they are
#: exempt from the no-nulls rule. Everything else in ``results`` stays strict.
VERBATIM_RESULT_KEYS = frozenset({"event_timeline"})

_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}

#: The project report's Appendix B targets, carried into the artifact so a
#: reader compares against the stated goal rather than a remembered one. They
#: are targets, not measurements, and the artifact keeps them apart.
REPORT_TARGETS_SECONDS = {"detection": 5.0, "recovery": 15.0}


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _stage_seconds(
    events: list[dict[str, Any]], *, killed_at: datetime, clock_offset: float
) -> dict[str, Any]:
    """Split recovery into the stages the orchestrator itself recorded.

    Event timestamps come from the orchestrator's clock and ``killed_at`` from
    this host's, so each difference is corrected by the measured offset between
    them. Every stage is measured from the kill, which is the moment the peer
    was really lost -- not from the last heartbeat, which would flatter
    detection by however long the peer had already been silent.
    """

    def since_kill(event: dict[str, Any]) -> float:
        delta = (_parse_ts(event["ts"]) - killed_at).total_seconds() - clock_offset
        return round(delta, 2)

    after = [e for e in events if _parse_ts(e["ts"]).timestamp() - clock_offset
             >= killed_at.timestamp()]
    detected = next((e for e in after if e["to_state"] == "REASSIGNED"), None)
    if detected is None:
        raise RuntimeError("no REASSIGNED event after the kill; nothing was detected")
    later = after[after.index(detected):]
    leased = next((e for e in later if e["to_state"] == "LEASED"), None)
    running = next((e for e in later if e["to_state"] == "RUNNING"), None)
    resumed = next(
        (e for e in later if "resumed_from_step" in (e.get("detail") or {})), None
    )
    if leased is None or running is None:
        raise RuntimeError("the job was reassigned but never ran again")

    stages: dict[str, Any] = {
        # Kill -> the detector declaring the peer gone and requeueing its job.
        "detection_seconds": since_kill(detected),
        # Kill -> a surviving peer holding the new lease.
        "relet_seconds": since_kill(leased),
        # Kill -> the replacement trainer actually running.
        "training_restarted_seconds": since_kill(running),
        "resumed_from_checkpoint": resumed is not None,
    }
    if resumed is not None:
        # Kill -> the replacement trainer having loaded the dead peer's
        # checkpoint: the point at which the lost work is genuinely recovered.
        stages["checkpoint_resume_seconds"] = since_kill(resumed)
        stages["resumed_from_step"] = resumed["detail"]["resumed_from_step"]
    return stages


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
    warmup = float(config.get("warmup_seconds", 20.0))
    scheduler = str(config.get("scheduler", "adaptive"))

    agent_a = await fleet.start_agent("recovery-a")
    agent_b = await fleet.start_agent("recovery-b")
    await client.wait_for_online_nodes(count=2, timeout_seconds=120.0)
    by_node = {
        agent_a.node_id: agent_a,
        agent_b.node_id: agent_b,
    }

    submit_started = datetime.now(UTC)
    job = await client.submit(spec=spec, scheduler_name=scheduler)
    submit_finished = datetime.now(UTC)
    job_id = job["id"]
    logger.info("submitted job %s under %s", job_id[:8], scheduler)

    # Wait for real training to actually be under way before killing anything,
    # so recovery has something to recover *from*.
    container = await await_trainer_container(job_id=job_id, timeout_seconds=job_timeout)
    running = await client.job(job_id)
    original_node = running.get("scheduled_node_id")
    victim = by_node.get(original_node)
    if victim is None:
        raise RuntimeError(
            f"job {job_id[:8]} was placed on node {original_node}, which is not "
            f"one of this run's agents — another agent is running against this "
            f"orchestrator and would confound the measurement"
        )

    logger.info(
        "job %s running on %s; letting it train for %.0fs",
        job_id[:8],
        victim.node_name,
        warmup,
    )
    deadline = time.monotonic() + warmup
    while time.monotonic() < deadline:
        state = (await client.job(job_id))["state"]
        if state in _TERMINAL:
            raise RuntimeError(
                f"job {job_id[:8]} reached {state} during warmup — it finished "
                f"before the failure could be induced, so nothing was measured. "
                f"Increase the job's epochs so it outlives the warmup."
            )
        await asyncio.sleep(1.0)

    killed_at = time.monotonic()
    killed_at_utc = datetime.now(UTC)
    fleet.kill_agent(victim)
    # Take the trainer down with the agent. On a real peer that vanishes — lid
    # closed, power lost — the container goes with the machine. Here only the
    # agent process died, so without this the orphaned trainer would keep
    # holding the one GPU that the *recovering* rank now needs, turning a
    # recovery measurement into a measurement of GPU contention. Killing it is
    # the faithful simulation, not a favour to the system: nobody is left alive
    # to report the exit, so the orchestrator still has to detect the loss on
    # its own.
    kill_container(container)
    logger.info(
        "SIGKILLed agent %s (%s) and its trainer %s; measuring recovery",
        victim.label,
        victim.node_name,
        container[:12],
    )

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

    # The orchestrator stamped QUEUED while the submit request was in flight,
    # so its clock minus the request's midpoint estimates the offset between
    # the two clocks (the orchestrator runs in a container whose clock can
    # drift from the host's). Half the round trip bounds the error.
    queued = next(e for e in completed["events"] if e["to_state"] == "QUEUED")
    midpoint = submit_started + (submit_finished - submit_started) / 2
    clock_offset = (_parse_ts(queued["ts"]) - midpoint).total_seconds()
    clock_offset_bound = (submit_finished - submit_started).total_seconds() / 2
    stages = _stage_seconds(
        completed["events"], killed_at=killed_at_utc, clock_offset=clock_offset
    )

    final_node = completed.get("scheduled_node_id")
    survivor = by_node.get(final_node)
    return {
        "job_id": job_id,
        "scheduler": scheduler,
        "failure_mode": "SIGKILL of the agent process holding the lease",
        "warmup_seconds": warmup,
        # The headline: real wall-clock from the peer vanishing to a real
        # completed job with a real accuracy.
        "recovery_seconds": round(recovery_seconds, 2),
        # Where that time went, each measured from the kill.
        "stages": stages,
        "report_targets_seconds": REPORT_TARGETS_SECONDS,
        "clock_offset_seconds": round(clock_offset, 3),
        "clock_offset_error_bound_seconds": round(clock_offset_bound, 3),
        "killed_node_id": original_node,
        "killed_node_name": victim.node_name,
        "completed_node_id": final_node,
        "completed_node_name": survivor.node_name if survivor else "unknown",
        "reassigned_to_a_different_node": bool(
            original_node is not None and final_node != original_node
        ),
        "final_lease_epoch": completed["current_lease_epoch"],
        "final_test_accuracy": final_accuracy,
        "final_loss": result.get("final_loss"),
        "epochs_completed": result.get("epochs_completed"),
        "device": result.get("device"),
        # The stage-by-stage record, so the single number above is inspectable:
        # detection, expiry, reassignment, re-lease, and completion each appear.
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
