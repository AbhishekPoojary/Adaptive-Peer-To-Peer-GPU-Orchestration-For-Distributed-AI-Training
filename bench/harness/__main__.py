"""Benchmark runner CLI.

    python -m bench.harness --scenario reliability_placement --password '...'

Runs a real scenario against a real orchestrator with real agents, and writes
one machine-generated artifact to ``bench/report/`` (CONTRIBUTING.md rule 6).

A run that cannot complete its measurements writes **nothing** and exits
non-zero. That is the whole design: an absent artifact honestly says "this did
not run", whereas a partial one — nulls where a metric belongs, a zero standing
in for "didn't happen" — invites a later reader to average over a hole.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from bench.harness.artifact import (
    IncompleteMeasurementError,
    build_artifact,
    write_artifact,
)
from bench.harness.client import BenchClient, OrchestratorError
from bench.harness.fleet import Fleet
from bench.harness.inventory import (
    DirtyWorktreeError,
    assess_limitations,
    capture_hardware,
    git_sha,
)
from bench.harness.scenarios import failure_recovery, reliability_placement

_REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_DIR = _REPO_ROOT / "bench" / "scenarios"

#: name -> scenario module. Each module exposes ``NAME``, ``run``, and
#: ``VERBATIM_RESULT_KEYS`` (which result keys hold a copy of the system's own
#: record rather than a measurement, and so may legitimately contain nulls).
SCENARIOS = {
    reliability_placement.NAME: reliability_placement,
    failure_recovery.NAME: failure_recovery,
}

logger = logging.getLogger("bench")


def load_scenario(name: str) -> dict[str, Any]:
    path = SCENARIO_DIR / f"{name}.json"
    if not path.exists():
        raise SystemExit(
            f"error: no scenario config at {path}. Available: "
            f"{sorted(p.stem for p in SCENARIO_DIR.glob('*.json'))}"
        )
    config: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return config


async def _run(args: argparse.Namespace) -> int:
    config = load_scenario(args.scenario)
    scenario_module = SCENARIOS[args.scenario]

    # Resolve provenance BEFORE running anything: discovering a dirty worktree
    # after a ten-minute benchmark wastes the run, and worse, tempts whoever is
    # waiting to pass --allow-dirty just to salvage it.
    try:
        sha = git_sha(allow_dirty=args.allow_dirty)
    except DirtyWorktreeError as exc:
        logger.error("%s", exc)
        return 2

    password = args.password or os.environ.get("BENCH_PASSWORD")
    if not password:
        logger.error(
            "no password: pass --password or set BENCH_PASSWORD. The benchmark "
            "drives the real authenticated API (ADR-012), so it needs an account."
        )
        return 2

    try:
        client = await BenchClient.login(
            base_url=args.orchestrator, username=args.username, password=password
        )
    except OrchestratorError as exc:
        logger.error("%s", exc)
        return 2

    # Preflight: a foreign agent already ONLINE can win a placement and silently
    # confound every measurement. The scenarios catch it, but only after
    # enrolling agents and submitting a job — check up front instead, and say
    # exactly what to do about it.
    try:
        strays = await client.online_nodes()
    except OrchestratorError as exc:
        logger.error("%s", exc)
        await client.aclose()
        return 2
    if strays and not args.allow_foreign_nodes:
        names = ", ".join(sorted(n["name"] for n in strays))
        logger.error(
            "%d node(s) are already ONLINE (%s). A benchmark run needs a fleet "
            "it fully controls, or a job may land on a node whose history and "
            "load this run did not create. Stop those agents and retry, or pass "
            "--allow-foreign-nodes if you know they are inert.",
            len(strays),
            names,
        )
        await client.aclose()
        return 2

    workdir = Path(tempfile.mkdtemp(prefix=f"bench-{args.scenario}-"))
    fleet = Fleet(client=client, workdir=workdir, orchestrator_url=args.orchestrator)
    logger.info("scenario=%s workdir=%s", args.scenario, workdir)

    try:
        results = await scenario_module.run(client=client, fleet=fleet, config=config)
    except (
        IncompleteMeasurementError,
        OrchestratorError,
        TimeoutError,
        RuntimeError,
    ) as exc:
        logger.error("scenario failed, writing no artifact: %s", exc)
        return 1
    finally:
        fleet.stop_all()
        await client.aclose()

    artifact = build_artifact(
        scenario=args.scenario,
        git_sha=sha,
        hardware=capture_hardware().as_dict(),
        results=results,
        limitations=assess_limitations(fleet.hostnames()).as_dict(),
        provisional=args.allow_dirty,
    )
    try:
        path = write_artifact(
            artifact,
            verbatim_keys=getattr(
                scenario_module, "VERBATIM_RESULT_KEYS", frozenset()
            ),
        )
    except IncompleteMeasurementError as exc:
        logger.error("refusing to publish an incomplete artifact: %s", exc)
        return 1

    logger.info("wrote %s", path)
    if args.keep_workdir:
        logger.info("agent logs kept at %s", workdir)
    else:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bench.harness",
        description="Run a benchmark scenario and write a measured artifact.",
    )
    parser.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    parser.add_argument("--orchestrator", default="http://localhost:8090")
    parser.add_argument("--username", default="abhishek")
    parser.add_argument(
        "--password",
        default=None,
        help="Operator password. Prefer the BENCH_PASSWORD environment variable.",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Run from a dirty worktree, marking the artifact provisional.",
    )
    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="Keep the temporary agent state dirs and logs for debugging.",
    )
    parser.add_argument(
        "--allow-foreign-nodes",
        action="store_true",
        help="Run even though nodes this run did not start are already ONLINE. "
        "They may take placements and confound the measurement.",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
