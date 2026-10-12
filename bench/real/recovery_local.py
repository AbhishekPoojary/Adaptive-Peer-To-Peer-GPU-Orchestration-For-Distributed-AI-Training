"""Failure recovery on the real fleet: this PC (node-03) is switched off mid-job.

Kills this machine's agent and trainer container while it trains, then reads
from the orchestrator's own records how long detection, reassignment and the
resume took. Writes bench/report/real/<stamp>-recovery-<label>.json.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(r"D:\P2P GPU ORCHESTRATION FOR ADAPTIVE AI TRAINING\gpu-orchestrator")
sys.path.insert(0, str(REPO))
from bench.real.experiment import REPORT_DIR, _client, _job_record, _ts  # noqa: E402

VICTIM = "node-03"
AGENT = [
    r"C:\Users\ABHISHEK\.gpu-orchestrator-agent-src\.venv\Scripts\python.exe", "-m", "agent",
    "--orchestrator", "https://api.172.198.136.166.sslip.io",
    "--state-dir", r"C:\Users\ABHISHEK\.gpu-orchestrator-agent",
    "--trainer-image", "abhisheks1290/gpu-orchestrator-trainer:latest",
]
SPEC = {"dataset": "cifar10", "model": "small_cnn", "epochs": 8, "batch_size": 128,
        "learning_rate": 0.001, "world_size": 1}


def kill_this_machine() -> float:
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and "
          "$_.CommandLine -match '-m agent' -and $_.CommandLine -match 'gpu-orchestrator-agent ' } "
          "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    killed_at = time.time()
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)
    ids = subprocess.run(["docker", "ps", "-q", "--filter",
                          "ancestor=abhisheks1290/gpu-orchestrator-trainer:latest"],
                         capture_output=True, text=True).stdout.split()
    if ids:
        subprocess.run(["docker", "kill", *ids], capture_output=True, check=False)
    return killed_at


def start_this_machine() -> None:
    # Stays open: the restarted agent outlives this script and keeps writing.
    log = open(REPO.parent / "agent-restarted.log", "ab")  # noqa: SIM115
    subprocess.Popen(AGENT, cwd=r"C:\Users\ABHISHEK\.gpu-orchestrator-agent-src",
                     stdout=log, stderr=subprocess.STDOUT,
                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)


async def main(reps: int, label: str) -> None:
    client = await _client()
    names = {n["id"]: n["name"] for n in await client.nodes()}
    trials = []
    try:
        for rep in range(reps):
            online = {n["name"] for n in await client.online_nodes()}
            while VICTIM not in online:
                await asyncio.sleep(3)
                online = {n["name"] for n in await client.online_nodes()}
            job = await client.submit(spec=SPEC, scheduler_name="adaptive")
            detail, _ = await client.wait_for_job_state(job["id"], states={"RUNNING"},
                                                        timeout_seconds=600)
            if names.get(detail["leases"][-1]["node_id"]) != VICTIM:
                print(f"rep {rep}: placed elsewhere, skipped", flush=True)
                await client.cancel(job["id"])
                continue
            await asyncio.sleep(25)
            killed_at = kill_this_machine()
            print(f"rep {rep}: killed {VICTIM} at {killed_at:.1f}", flush=True)
            resumed = None
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                d = await client.job(job["id"])
                lines = [e for e in await client.logs(job["id"]) if "RESUMED from" in e["line"]]
                if lines:
                    resumed = lines[0]
                    break
                await asyncio.sleep(2)
            d = await client.job(job["id"])
            events = d["events"]
            reassigned = next((_ts(e["ts"]) for e in events if e["to_state"] == "REASSIGNED"), None)
            rerun = [_ts(e["ts"]) for e in events if e["to_state"] == "RUNNING"]
            survivor = names.get(d["leases"][-1]["node_id"])
            step = None
            if resumed:
                text = resumed["line"]
                if "global_step=" in text:
                    step = int(text.split("global_step=")[1].split()[0])
            trial = {
                "rep": rep, "victim": VICTIM, "survivor": survivor, "killed_at": killed_at,
                "detect_s": reassigned - killed_at if reassigned else None,
                "restart_s": rerun[1] - killed_at if len(rerun) > 1 else None,
                "resumed_s": _ts(resumed["ts"]) - killed_at if resumed else None,
                "resumed_from_step": step,
                "detail": next((e["detail"].get("message") for e in events
                                if e["to_state"] == "REASSIGNED"), None),
                **_job_record(d, names),
            }
            trials.append(trial)
            print(json.dumps({k: trial[k] for k in ("rep", "survivor", "detect_s",
                                                     "restart_s", "resumed_s",
                                                     "resumed_from_step")}), flush=True)
            await client.cancel(job["id"])
            start_this_machine()
            await asyncio.sleep(20)
    finally:
        await client.aclose()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    path = REPORT_DIR / f"{stamp}-recovery-{label}.json"
    path.write_text(json.dumps({"kind": "recovery", "label": label, "spec": SPEC,
                                "method": "agent and trainer container killed on the "
                                          "victim; times from the orchestrator's records",
                                "trials": trials}, indent=1), encoding="utf-8")
    print(f"saved {path}", flush=True)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), sys.argv[2]))
