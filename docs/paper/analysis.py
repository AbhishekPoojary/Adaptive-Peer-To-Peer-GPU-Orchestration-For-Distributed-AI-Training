"""Every number and figure in the manuscript, computed from bench/report/real.

    python docs/paper/analysis.py

Writes docs/paper/figures/*.png and docs/paper/numbers.json. Nothing here is
typed in by hand: re-running it on the same files gives the same paper numbers.
"""

from __future__ import annotations

import csv
import json
import math
import statistics as st
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from scipy.stats import mannwhitneyu  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
REAL = ROOT / "bench" / "report" / "real"
OUT = Path(__file__).resolve().parent
FIG = OUT / "figures"
FIG.mkdir(exist_ok=True)
plt.rcParams.update({"font.family": "serif", "font.size": 9, "figure.dpi": 200})


def summary(values: list[float]) -> dict[str, float]:
    sd = st.stdev(values) if len(values) > 1 else 0.0
    return {"n": len(values), "mean": st.fmean(values), "sd": sd,
            "ci95": 1.96 * sd / math.sqrt(len(values)) if values else 0.0,
            "median": st.median(values), "min": min(values), "max": max(values)}


numbers: dict[str, object] = {}

# --- Placement and ablation -------------------------------------------------
placement = json.loads(next(REAL.glob("*placement*.json")).read_text(encoding="utf-8"))
jct: dict[str, list[float]] = {}
on_gpu: dict[str, int] = {}
for job in placement["jobs"]:
    jct.setdefault(job["scheduler"], []).append(job["jct_s"])
    on_gpu[job["scheduler"]] = on_gpu.get(job["scheduler"], 0) + ("node-03" in job["nodes"])
order = ["adaptive", "adaptive_no_load", "adaptive_no_reliability", "adaptive_no_latency",
         "adaptive_no_speed", "least_loaded", "round_robin"]
numbers["placement"] = {
    s: {**summary(jct[s]), "on_gpu": on_gpu[s],
        "p_vs_adaptive": None if s == "adaptive"
        else mannwhitneyu(jct["adaptive"], jct[s], alternative="less").pvalue}
    for s in order
}
labels = {"adaptive": "Adaptive (full)", "adaptive_no_load": "Adaptive w/o load",
          "adaptive_no_reliability": "Adaptive w/o reliability",
          "adaptive_no_latency": "Adaptive w/o latency",
          "adaptive_no_speed": "Adaptive w/o speed", "least_loaded": "Least loaded",
          "round_robin": "Round robin"}
fig, ax = plt.subplots(figsize=(3.5, 2.3))
means = [numbers["placement"][s]["mean"] for s in order]  # type: ignore[index]
cis = [numbers["placement"][s]["ci95"] for s in order]  # type: ignore[index]
colors = ["#1f4e79"] + ["#7fa7cf"] * 4 + ["#bbbbbb"] * 2
ax.barh(range(len(order)), means, xerr=cis, color=colors, capsize=2, linewidth=0)
ax.set_yticks(range(len(order)), [labels[s] for s in order], fontsize=7)
ax.invert_yaxis()
ax.set_xlabel("Job completion time (s)")
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout(pad=0.4)
fig.savefig(FIG / "fig_placement.png")
plt.close(fig)

# --- Failure recovery -------------------------------------------------------
trials = [t for f in REAL.glob("*recovery*.json")
          for t in json.loads(f.read_text(encoding="utf-8"))["trials"]]
numbers["recovery"] = {k: summary([t[k] for t in trials]) for k in
                       ("detect_s", "restart_s", "resumed_s")}
numbers["recovery"]["resumed_from_checkpoint"] = sum(  # type: ignore[index]
    t["resumed_from_step"] is not None for t in trials)
numbers["recovery"]["trials"] = len(trials)  # type: ignore[index]
numbers["recovery"]["resumed_steps"] = sorted(t["resumed_from_step"] for t in trials)  # type: ignore[index]
fig, ax = plt.subplots(figsize=(3.5, 1.9))
data = [[t[k] for t in trials] for k in ("detect_s", "restart_s", "resumed_s")]
ax.boxplot(data, vert=False, widths=0.5, medianprops={"color": "#1f4e79"})
ax.set_yticks([1, 2, 3], ["Failure\ndetected", "Restarted\non survivor", "Resumed from\ncheckpoint"],
              fontsize=7)
ax.set_xlabel("Seconds after the machine was killed")
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout(pad=0.4)
fig.savefig(FIG / "fig_recovery.png")
plt.close(fig)

# --- Scaling (makespan of a 12-job queue) -----------------------------------
scaling = {}
for f in sorted(REAL.glob("*scaling*.json")):
    d = json.loads(f.read_text(encoding="utf-8"))
    if d["completed"] == 12:
        scaling[d["label"]] = {"machines": d["machines"], "makespan_s": d["makespan_s"],
                               "scheduler": d["scheduler"],
                               "extra_attempts": sum(j["attempts"] - 1 for j in d["jobs"]),
                               "per_node": {n: sum(n in j["nodes"][-1:] for j in d["jobs"])
                                            for n in ("node-02", "node-03")}}
numbers["scaling"] = scaling

# --- Heartbeat inter-arrival gaps and the detector trade-off -----------------
rows = list(csv.DictReader((REAL / "20261012-heartbeats.csv").open(encoding="utf-8")))
by_node: dict[str, list[float]] = {}
for r in rows:
    ts = datetime.fromisoformat(r["ts"].replace("+00", "+00:00")).timestamp()
    by_node.setdefault(r["node"], []).append(ts)
gaps: dict[str, list[float]] = {}
for node, stamps in by_node.items():
    stamps.sort()
    # A gap over 120 s is the machine leaving (lab session ended), not jitter.
    gaps[node] = [b - a for a, b in zip(stamps, stamps[1:], strict=False) if 0 < b - a < 120]
hours = sum(sum(g) for g in gaps.values()) / 3600
numbers["heartbeats"] = {
    "nodes": len(gaps), "intervals": sum(len(g) for g in gaps.values()),
    "node_hours": hours,
    "per_node": {n: {"p50": st.median(g), "p99": sorted(g)[int(0.99 * len(g))],
                     "max": max(g), "over5": sum(x > 5 for x in g)}
                 for n, g in sorted(gaps.items())},
}


def phi(elapsed: float, mean: float, std: float) -> float:
    p_later = 0.5 * math.erfc((elapsed - mean) / (std * math.sqrt(2)))
    return -math.log10(max(p_later, 1e-12))


def evaluate(pause: float, threshold: float = 3.0, floor: float = 3.0,
             window: int = 20, min_std: float = 0.5) -> tuple[float, float]:
    """(false suspicions per node-hour, median detection time of a real crash),
    replaying the shipped φ-accrual detector over the real arrival times."""
    false, detect = 0, []
    for g in gaps.values():
        for i in range(window, len(g)):
            hist = g[i - window:i]
            mean = st.fmean(hist) + pause
            std = max(st.pstdev(hist), min_std)
            # Smallest silence at which this detector would declare failure.
            lo, hi = 0.0, 600.0
            for _ in range(40):
                mid = (lo + hi) / 2
                if phi(mid, mean, std) >= threshold:
                    hi = mid
                else:
                    lo = mid
            trigger = max(hi, floor)
            detect.append(trigger)
            if g[i] > trigger:
                false += 1  # the node was alive: the next heartbeat did arrive
    return false / hours, st.median(detect)


pauses = [0.0, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 10.0, 12.0, 15.0]
curve = [(p, *evaluate(p)) for p in pauses]
numbers["detector_curve"] = [{"pause_s": p, "false_per_node_hour": f, "median_detect_s": d}
                             for p, f, d in curve]
fig, ax = plt.subplots(figsize=(3.5, 2.3))
ax.plot([c[2] for c in curve], [c[1] for c in curve], "o-", color="#1f4e79", markersize=3)
for p, f, d in curve:
    if p in (0.0, 2.0, 8.0, 15.0):
        ax.annotate(f"{p:g} s", (d, f), textcoords="offset points", xytext=(4, 3), fontsize=6.5)
ax.set_xlabel("Median time to detect a crash (s)")
ax.set_ylabel("False suspicions / node-hour")
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout(pad=0.4)
fig.savefig(FIG / "fig_detector.png")
plt.close(fig)

(OUT / "numbers.json").write_text(json.dumps(numbers, indent=1, default=float), encoding="utf-8")
print(json.dumps(numbers, indent=1, default=float)[:6000])
