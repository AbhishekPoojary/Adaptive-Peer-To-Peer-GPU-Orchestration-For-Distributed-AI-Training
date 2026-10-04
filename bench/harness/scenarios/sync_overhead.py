"""What share of a distributed training step does gradient synchronization cost?

The report targets synchronization overhead below 20% of training time. This
runs a real world_size=2 job through the orchestrator -- two real agents, a
real c10d rendezvous, real DDP -- and reads the measurement the trainer takes
at the start of every distributed job (``_profile_sync``): the same forward and
backward passes timed with the gradient all-reduce and under ``no_sync()``
without it. The difference is what synchronization adds to a step after any
overlap with the backward pass.

What it is not: two machines. Both ranks run on this host, in containers on one
Docker network, using the gloo backend on CPU (two GPU processes cannot share
this laptop's single GPU, ADR-010). The all-reduce therefore crosses a local
bridge, not a network; on real separate machines it would cost more. The
artifact records this next to the number.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from bench.harness.client import BenchClient
from bench.harness.fleet import Fleet

logger = logging.getLogger("bench.sync_overhead")

NAME = "sync_overhead"

#: Every value here is a measurement (the trainer's own, read from its log).
VERBATIM_RESULT_KEYS: frozenset[str] = frozenset()

REPORT_TARGET_SHARE = 0.20


async def run(*, client: BenchClient, fleet: Fleet, config: dict[str, Any]) -> dict[str, Any]:
    """Execute the scenario and return its measured results."""
    spec = dict(config["job_spec"])
    timeout = float(config.get("job_timeout_seconds", 1800.0))
    await fleet.start_agent("sync-a")
    await fleet.start_agent("sync-b")
    await client.wait_for_online_nodes(count=2, timeout_seconds=120.0)

    job = await client.submit(spec=spec, scheduler_name="least_loaded")
    done, _ = await client.wait_for_job_state(
        job["id"], states={"COMPLETED", "FAILED"}, timeout_seconds=timeout
    )
    if done["state"] != "COMPLETED":
        raise RuntimeError(f"the distributed job ended {done['state']}")

    profile = None
    for line in await client.logs(job["id"]):
        text = line["line"].strip()
        if text.startswith("{") and '"sync_profile"' in text:
            profile = json.loads(text)
    if profile is None:
        raise RuntimeError("rank 0 logged no sync_profile line; nothing was measured")
    profile.pop("type", None)

    share = float(profile["sync_share_of_step"])
    result = done.get("result") or {}
    return {
        "job_id": job["id"],
        "world_size": spec["world_size"],
        "measured": profile,
        "report_target_share": REPORT_TARGET_SHARE,
        "meets_report_target": share < REPORT_TARGET_SHARE,
        "final_test_accuracy": result.get("final_test_accuracy"),
        "topology": (
            "two ranks on one host, in containers on one Docker bridge network, "
            "gloo on CPU; not two machines"
        ),
    }
