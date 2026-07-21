"""Unit tests for the scheduler hard filters and baseline strategies (ADR-009).

Pure tests over transient ORM objects — no database. They pin the placement
contract: filters reject ineligible nodes, RoundRobin cycles fairly, and
LeastLoaded picks the truly least-loaded node from real telemetry while
deprioritising a node that has never reported a sample (never assuming idle).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.models.job import Job, JobState
from orchestrator.models.node import Node, NodeStatus, NodeTelemetrySample
from orchestrator.schedulers.base import (
    NodeSnapshot,
    current_load,
    eligible_candidates,
    passes_hard_filters,
)
from orchestrator.schedulers.least_loaded import LeastLoadedScheduler
from orchestrator.schedulers.round_robin import RoundRobinScheduler

_NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
_STALE = 15.0
_GB = 1024**3


def _node(
    name: str,
    *,
    status: NodeStatus = NodeStatus.ONLINE,
    heartbeat_age_s: float = 1.0,
    vram_bytes: int | None = 8 * _GB,
    last_assigned_at: datetime | None = None,
) -> Node:
    gpus = (
        [{"name": "T", "vram_bytes": vram_bytes, "driver_version": "x"}]
        if vram_bytes is not None
        else []
    )
    return Node(
        id=uuid.uuid4(),
        name=name,
        public_key="pem",
        status=status,
        last_heartbeat_at=_NOW - timedelta(seconds=heartbeat_age_s),
        hardware={"hostname": name, "os": "t", "cpu_model": "t", "cores": 4,
                  "ram_bytes": 16 * _GB, "gpus": gpus},
        agent_version="t",
        last_assigned_at=last_assigned_at,
    )


def _sample(*, cpu: float, gpu_util: float | None) -> NodeTelemetrySample:
    gpu = (
        [{"util_percent": gpu_util, "mem_used_bytes": 0, "mem_total_bytes": _GB,
          "temperature_c": None, "power_w": None}]
        if gpu_util is not None
        else None
    )
    return NodeTelemetrySample(
        cpu_percent=cpu, ram_used_bytes=0, ram_total_bytes=_GB, gpu=gpu,
        rtt_ms=None, rtt_ewma_ms=None,
    )


def _snap(node: Node, sample: NodeTelemetrySample | None, *, busy: bool = False) -> NodeSnapshot:
    return NodeSnapshot(node=node, latest_sample=sample, has_active_lease=busy)


def _passes(job: Job, snap: NodeSnapshot) -> bool:
    return passes_hard_filters(job, snap, now=_NOW, stale_seconds=_STALE)


def _job(*, min_gpu_mem_bytes: int | None) -> Job:
    return Job(
        id=uuid.uuid4(),
        spec={"dataset": "mnist", "model": "m", "epochs": 1, "batch_size": 8,
              "learning_rate": 0.1, "world_size": 1, "min_gpu_mem_bytes": min_gpu_mem_bytes},
        scheduler_name="round_robin",
        state=JobState.QUEUED,
        submitted_by="t",
    )


# --- Hard filters ------------------------------------------------------------


def test_offline_node_filtered_out() -> None:
    snap = _snap(_node("n", status=NodeStatus.OFFLINE), _sample(cpu=1, gpu_util=1))
    assert not _passes(_job(min_gpu_mem_bytes=None), snap)


def test_draining_node_filtered_out() -> None:
    snap = _snap(_node("n", status=NodeStatus.DRAINING), _sample(cpu=1, gpu_util=1))
    assert not _passes(_job(min_gpu_mem_bytes=None), snap)


def test_stale_heartbeat_filtered_out() -> None:
    snap = _snap(_node("n", heartbeat_age_s=999), _sample(cpu=1, gpu_util=1))
    assert not _passes(_job(min_gpu_mem_bytes=None), snap)


def test_busy_node_filtered_out() -> None:
    snap = _snap(_node("n"), _sample(cpu=1, gpu_util=1), busy=True)
    assert not _passes(_job(min_gpu_mem_bytes=None), snap)


def test_insufficient_vram_filtered_out() -> None:
    snap = _snap(_node("n", vram_bytes=4 * _GB), _sample(cpu=1, gpu_util=1))
    assert not _passes(_job(min_gpu_mem_bytes=8 * _GB), snap)


def test_sufficient_vram_passes() -> None:
    snap = _snap(_node("n", vram_bytes=8 * _GB), _sample(cpu=1, gpu_util=1))
    assert _passes(_job(min_gpu_mem_bytes=8 * _GB), snap)


def test_cpu_only_node_ineligible_for_gpu_job_but_ok_for_cpu_job() -> None:
    cpu_node = _snap(_node("cpu", vram_bytes=None), _sample(cpu=1, gpu_util=None))
    assert not _passes(_job(min_gpu_mem_bytes=4 * _GB), cpu_node)
    assert _passes(_job(min_gpu_mem_bytes=None), cpu_node)


# --- current_load ------------------------------------------------------------


def test_current_load_prefers_gpu_util() -> None:
    snap = _snap(_node("n"), _sample(cpu=90.0, gpu_util=10.0))
    assert current_load(snap) == 10.0


def test_current_load_falls_back_to_cpu_when_no_gpu() -> None:
    snap = _snap(_node("n", vram_bytes=None), _sample(cpu=42.0, gpu_util=None))
    assert current_load(snap) == 42.0


def test_current_load_is_none_without_sample() -> None:
    assert current_load(_snap(_node("n"), None)) is None


# --- RoundRobin --------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_robin_prefers_never_assigned_then_oldest() -> None:
    rr = RoundRobinScheduler()
    a = _node("a", last_assigned_at=_NOW - timedelta(seconds=5))
    b = _node("b", last_assigned_at=None)  # never assigned -> goes first
    c = _node("c", last_assigned_at=_NOW - timedelta(seconds=50))
    cands = [_snap(a, _sample(cpu=1, gpu_util=1)),
             _snap(b, _sample(cpu=1, gpu_util=1)),
             _snap(c, _sample(cpu=1, gpu_util=1))]
    first = await rr.select_node(_job(min_gpu_mem_bytes=None), cands)
    assert first is not None and first.node.name == "b"

    # With b now assigned (most recent), the oldest real timestamp (c) is next.
    b.last_assigned_at = _NOW
    nxt = await rr.select_node(_job(min_gpu_mem_bytes=None), cands)
    assert nxt is not None and nxt.node.name == "c"


@pytest.mark.asyncio
async def test_round_robin_none_when_no_candidates() -> None:
    assert await RoundRobinScheduler().select_node(_job(min_gpu_mem_bytes=None), []) is None


# --- LeastLoaded -------------------------------------------------------------


@pytest.mark.asyncio
async def test_least_loaded_picks_lowest_real_load() -> None:
    ll = LeastLoadedScheduler()
    busy = _snap(_node("busy"), _sample(cpu=1, gpu_util=80.0))
    idle = _snap(_node("idle"), _sample(cpu=1, gpu_util=5.0))
    mid = _snap(_node("mid"), _sample(cpu=1, gpu_util=40.0))
    chosen = await ll.select_node(_job(min_gpu_mem_bytes=None), [busy, idle, mid])
    assert chosen is not None and chosen.node.name == "idle"


@pytest.mark.asyncio
async def test_least_loaded_deprioritises_node_without_telemetry() -> None:
    ll = LeastLoadedScheduler()
    # A node with no sample must NOT be treated as 0%/idle — the node with a
    # real (even high-ish) reading wins over the unknown one.
    no_sample = _snap(_node("unknown"), None)
    known = _snap(_node("known"), _sample(cpu=1, gpu_util=55.0))
    chosen = await ll.select_node(_job(min_gpu_mem_bytes=None), [no_sample, known])
    assert chosen is not None and chosen.node.name == "known"


@pytest.mark.asyncio
async def test_least_loaded_uses_no_telemetry_node_only_as_last_resort() -> None:
    ll = LeastLoadedScheduler()
    no_sample = _snap(_node("only"), None)
    chosen = await ll.select_node(_job(min_gpu_mem_bytes=None), [no_sample])
    assert chosen is not None and chosen.node.name == "only"


def test_eligible_candidates_filters_the_set() -> None:
    ok = _snap(_node("ok"), _sample(cpu=1, gpu_util=1))
    off = _snap(_node("off", status=NodeStatus.OFFLINE), _sample(cpu=1, gpu_util=1))
    elig = eligible_candidates(
        _job(min_gpu_mem_bytes=None), [ok, off], now=_NOW, stale_seconds=_STALE
    )
    assert [s.node.name for s in elig] == ["ok"]
