"""Experiments on a real fleet: the live orchestrator and real machines.

The harness in ``bench/harness`` starts its own agents on one host. This runs
against an orchestrator other people's machines have joined, and only ever
submits jobs and reads results through the public API -- nothing is simulated.
Each run writes one JSON file to ``bench/report/real/``; ``stats`` turns a set
of them into the tables for the paper.

    python -m bench.real.experiment scaling   --label n3 --jobs 12
    python -m bench.real.experiment placement --schedulers adaptive,round_robin --reps 10
    python -m bench.real.experiment recovery  --reps 5
    python -m bench.real.experiment stats

Connection: ORCH_URL (e.g. https://api.172.198.136.166.sslip.io), ORCH_USER and
ORCH_PASSWORD for an account on it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bench.harness.client import BenchClient

REPORT_DIR = Path(__file__).resolve().parents[1] / "report" / "real"
TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")


def _ts(value: str | None) -> float | None:
    return datetime.fromisoformat(value).timestamp() if value else None


def _spec(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "dataset": args.dataset,
        "model": args.model,
        "epochs": args.epochs,
        "batch_size": args.batch_size,
        "learning_rate": args.lr,
        "world_size": args.world_size,
        "sync_every": args.sync_every,
    }


async def _client() -> BenchClient:
    return await BenchClient.login(
        base_url=os.environ["ORCH_URL"].rstrip("/"),
        username=os.environ["ORCH_USER"],
        password=os.environ["ORCH_PASSWORD"],
        timeout=60.0,
    )


async def _wait_all(
    client: BenchClient, job_ids: list[str], timeout: float
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while True:
        jobs = [await client.job(j) for j in job_ids]
        done = sum(j["state"] in TERMINAL for j in jobs)
        print(f"  {done}/{len(jobs)} finished", flush=True)
        if done == len(jobs) or time.monotonic() > deadline:
            return jobs
        await asyncio.sleep(10)


def _job_record(job: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """What one job contributes to the results: times, placement, outcome."""
    submitted = _ts(job["submitted_at"])
    started, finished = _ts(job["started_at"]), _ts(job["finished_at"])
    nodes = sorted({names.get(lease["node_id"], lease["node_id"]) for lease in job["leases"]})
    return {
        "id": job["id"],
        "state": job["state"],
        "scheduler": job["scheduler_name"],
        "nodes": nodes,
        "attempts": job["current_lease_epoch"],
        "wait_s": started - submitted if started and submitted else None,
        "jct_s": finished - submitted if finished and submitted else None,
        "submitted": submitted,
        "finished": finished,
        "accuracy": (job.get("result") or {}).get("final_test_accuracy"),
    }


async def _fleet(client: BenchClient) -> tuple[list[dict[str, Any]], dict[str, str]]:
    nodes = await client.online_nodes()
    fleet = [
        {
            "name": n["name"],
            "gpus": [g["name"] for g in n["hardware"]["gpus"]],
            "cpu": n["hardware"]["cpu_model"],
            "cores": n["hardware"]["cores"],
            "enrolled_by": n.get("enrolled_by"),
        }
        for n in nodes
    ]
    names = {n["id"]: n["name"] for n in await client.nodes()}
    return fleet, names


def _save(kind: str, label: str, payload: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    path = REPORT_DIR / f"{stamp}-{kind}-{label}.json"
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"saved {path}")
    return path


async def scaling(args: argparse.Namespace) -> None:
    """Makespan of a fixed queue of jobs on however many machines are online."""
    client = await _client()
    try:
        fleet, names = await _fleet(client)
        print(f"{len(fleet)} machines online: {[f['name'] for f in fleet]}")
        ids = [
            (await client.submit(spec=_spec(args), scheduler_name=args.scheduler))["id"]
            for _ in range(args.jobs)
        ]
        jobs = await _wait_all(client, ids, args.timeout)
        records = [_job_record(j, names) for j in jobs]
        done = [r for r in records if r["state"] == "COMPLETED"]
        makespan = (
            max(r["finished"] for r in done) - min(r["submitted"] for r in records)
            if done else None
        )
        _save("scaling", args.label, {
            "kind": "scaling", "label": args.label, "machines": len(fleet), "fleet": fleet,
            "spec": _spec(args), "scheduler": args.scheduler, "jobs": records,
            "completed": len(done), "makespan_s": makespan,
            "jobs_per_hour": len(done) / makespan * 3600 if makespan else None,
        })
    finally:
        await client.aclose()


async def placement(args: argparse.Namespace) -> None:
    """One job at a time, schedulers interleaved, so each choice sees the same fleet."""
    client = await _client()
    try:
        fleet, names = await _fleet(client)
        records = []
        for rep in range(args.reps):
            for scheduler in args.schedulers.split(","):
                job = await client.submit(spec=_spec(args), scheduler_name=scheduler)
                (done,) = await _wait_all(client, [job["id"]], args.timeout)
                record = {"rep": rep, **_job_record(done, names)}
                print(f"  rep {rep} {scheduler}: {record['nodes']} jct={record['jct_s']}")
                records.append(record)
        _save("placement", args.label, {
            "kind": "placement", "label": args.label, "fleet": fleet,
            "spec": _spec(args), "jobs": records,
        })
    finally:
        await client.aclose()


async def recovery(args: argparse.Namespace) -> None:
    """A person switches the training machine off; the times come from the server."""
    client = await _client()
    try:
        fleet, names = await _fleet(client)
        trials = []
        for rep in range(args.reps):
            job = await client.submit(spec=_spec(args), scheduler_name=args.scheduler)
            await client.wait_for_job_state(
                job["id"], states={"RUNNING"}, timeout_seconds=args.timeout
            )
            await asyncio.sleep(args.run_before_kill)
            detail = await client.job(job["id"])
            victim_id = detail["leases"][-1]["node_id"]
            input(f"\n>>> rep {rep}: switch OFF {names.get(victim_id, victim_id)} "
                  "now (close its window or unplug its network), then press Enter ")
            killed_at = time.time()
            (final,) = await _wait_all(client, [job["id"]], args.timeout)
            events = {e["to_state"]: _ts(e["ts"]) for e in final["events"]}
            node = next((n for n in await client.nodes() if n["id"] == victim_id), None)
            last_beat = _ts(node["last_heartbeat_at"]) if node else None
            reassigned = events.get("REASSIGNED")
            logs = [entry["line"] for entry in await client.logs(job["id"])]
            trials.append({
                "rep": rep, "victim": names.get(victim_id), **_job_record(final, names),
                "detect_s": reassigned - last_beat if reassigned and last_beat else None,
                "detect_from_kill_s": reassigned - killed_at if reassigned else None,
                "resumed_from_checkpoint": any("RESUMED from" in m for m in logs),
            })
            print(f"  rep {rep}: {trials[-1]}")
            input(">>> switch that machine back ON (rerun its join command), then press Enter ")
        _save("recovery", args.label, {
            "kind": "recovery", "label": args.label, "fleet": fleet,
            "spec": _spec(args), "trials": trials,
        })
    finally:
        await client.aclose()


def _summary(values: list[float]) -> str:
    if not values:
        return "n=0"
    mean = statistics.fmean(values)
    if len(values) < 2:
        return f"{mean:.1f} (n=1)"
    sd = statistics.stdev(values)
    half = 1.96 * sd / len(values) ** 0.5  # shortcut: normal approx, fine for n >= ~10
    return f"{mean:.1f} ± {sd:.1f} (95% CI ±{half:.1f}, n={len(values)})"


def stats(_args: argparse.Namespace) -> None:
    """Tables over every saved run: mean ± sd, and Mann-Whitney U against adaptive."""
    from scipy.stats import mannwhitneyu

    runs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(REPORT_DIR.glob("*.json"))]
    print("\n## Scaling (throughput vs machines)")
    for run in (r for r in runs if r["kind"] == "scaling"):
        print(f"- {run['label']}: {run['machines']} machines, {run['completed']} jobs, "
              f"makespan {run['makespan_s'] or 0:.0f} s, {run['jobs_per_hour'] or 0:.1f} jobs/h")

    print("\n## Placement (job completion time, s)")
    by_scheduler: dict[str, list[float]] = {}
    for run in (r for r in runs if r["kind"] == "placement"):
        for job in run["jobs"]:
            if job["jct_s"] is not None and job["state"] == "COMPLETED":
                by_scheduler.setdefault(job["scheduler"], []).append(job["jct_s"])
    for name, values in sorted(by_scheduler.items()):
        line = f"- {name}: {_summary(values)}"
        base = by_scheduler.get("adaptive")
        if name != "adaptive" and base and len(base) > 1 and len(values) > 1:
            p = mannwhitneyu(base, values, alternative="less").pvalue
            line += f"; adaptive faster: p={p:.4f}"
        print(line)

    print("\n## Recovery")
    trials = [t for r in runs if r["kind"] == "recovery" for t in r["trials"]]
    for key in ("detect_s", "detect_from_kill_s", "jct_s"):
        print(f"- {key}: {_summary([t[key] for t in trials if t[key] is not None])}")
    resumed = sum(t["resumed_from_checkpoint"] for t in trials)
    print(f"- resumed from checkpoint: {resumed}/{len(trials)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("scaling", "placement", "recovery"):
        p = sub.add_parser(name)
        p.add_argument("--label", default="run")
        p.add_argument("--dataset", default="cifar10")
        p.add_argument("--model", default="small_cnn")
        p.add_argument("--epochs", type=int, default=2)
        p.add_argument("--batch-size", type=int, default=128)
        p.add_argument("--lr", type=float, default=0.001)
        p.add_argument("--world-size", type=int, default=1)
        p.add_argument("--sync-every", type=int, default=1)
        p.add_argument("--scheduler", default="adaptive")
        p.add_argument("--timeout", type=float, default=3600)
    sub.choices["scaling"].add_argument("--jobs", type=int, default=12)
    sub.choices["placement"].add_argument(
        "--schedulers", default="adaptive,adaptive_no_load,adaptive_no_reliability,"
        "adaptive_no_latency,least_loaded,round_robin")
    sub.choices["placement"].add_argument("--reps", type=int, default=10)
    sub.choices["recovery"].add_argument("--reps", type=int, default=5)
    sub.choices["recovery"].add_argument("--run-before-kill", type=float, default=30)
    sub.add_parser("stats")
    args = parser.parse_args()
    if args.command == "stats":
        stats(args)
    else:
        commands = {"scaling": scaling, "placement": placement, "recovery": recovery}
        asyncio.run(commands[args.command](args))


if __name__ == "__main__":
    main()
