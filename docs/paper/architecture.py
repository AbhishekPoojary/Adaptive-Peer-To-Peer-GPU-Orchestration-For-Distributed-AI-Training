"""Fig. 1 of the manuscript: the system's architecture (drawn, not measured)."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

plt.rcParams.update({"font.family": "serif", "font.size": 7, "figure.dpi": 220})
fig, ax = plt.subplots(figsize=(7.0, 3.1))
ax.set_xlim(0, 100)
ax.set_ylim(0, 44)
ax.axis("off")


def box(x, y, w, h, title, lines, color):  # type: ignore[no-untyped-def]
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.4,rounding_size=1.2",
                                linewidth=0.8, edgecolor="#333333", facecolor=color))
    ax.text(x + w / 2, y + h - 2.2, title, ha="center", va="top", fontweight="bold")
    for i, line in enumerate(lines):
        ax.text(x + w / 2, y + h - 5.6 - 3.0 * i, line, ha="center", va="top", fontsize=6.3)


def arrow(a, b, label="", rad=0.0, offset=(0, 1.2)):  # type: ignore[no-untyped-def]
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle="-|>", mutation_scale=7, linewidth=0.7,
                                 color="#333333", connectionstyle=f"arc3,rad={rad}"))
    if label:
        ax.text((a[0] + b[0]) / 2 + offset[0], (a[1] + b[1]) / 2 + offset[1], label,
                ha="center", fontsize=5.8, style="italic")


box(2, 28, 20, 14, "Users (browser)",
    ["upload dataset", "submit job, watch,", "download model"], "#eef3f8")
box(34, 6.5, 32, 35.5, "Orchestrator (cloud VM)",
    ["REST + WebSocket API, auth, ownership",
     "Scheduler: adaptive score",
     "S = aL - bR + gD + dC",
     "+ minimum completion time",
     "Lease manager: epochs, fencing",
     "phi-accrual failure detector",
     "PostgreSQL (state, audit)",
     "Object store (datasets, checkpoints)"], "#e8f1e8")
for i, (title, hw) in enumerate([("Volunteer A", "GPU, Docker sandbox"),
                                 ("Volunteer B", "CPU only"),
                                 ("Volunteer C", "GPU, process mode")]):
    box(78, 31 - 12.5 * i, 20, 10.0, title, [hw, "agent + trainer"], "#fbf1e6")
arrow((22.6, 35), (33.4, 35), "HTTPS", offset=(0, 1.2))
for i in range(3):
    y = 36 - 12.5 * i
    arrow((77.4, y), (66.6, y - 1.5))
ax.text(72, 27.8, "\n".join(["outbound only", "(NAT-safe):", "heartbeat,", "lease claim,",
                             "logs, metrics"]),
        ha="center", va="center", fontsize=5.6, style="italic")
ax.text(50, 1.2, "Datasets and checkpoints stream through the orchestrator under lease-scoped "
                 "tokens; volunteers never hold storage credentials.",
        ha="center", fontsize=5.8, style="italic")
fig.tight_layout(pad=0.2)
fig.savefig(Path(__file__).resolve().parent / "figures" / "fig_architecture.png")
