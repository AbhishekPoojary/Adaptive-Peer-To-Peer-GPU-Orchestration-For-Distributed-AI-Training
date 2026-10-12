"""The adaptive score's speed term: placement learns how fast each machine trains.

The first real-fleet run placed a CIFAR-10 job on a CPU-only laptop (102 s)
over two idle GPU laptops (~20 s): load, reliability and latency all favoured
it and nothing in the score knew a GPU is faster. These tests replay that
decision and pin down how speed is learned.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.models.lease import Lease, LeaseState
from orchestrator.schedulers.adaptive import (
    CandidateReliability,
    score_candidates,
    select_best,
)
from tests.test_adaptive_scheduler import (
    _add_outcome,
    _add_sample,
    _decide,
    _make_node,
    _placeable_job,
    _settings,
)


def _cand(name: str, util: float, rtt: float, ws: float, wf: float, speed: float | None):
    sample = SimpleNamespace(rtt_ewma_ms=rtt, cpu_percent=util, gpu=None)
    snapshot = SimpleNamespace(node=SimpleNamespace(name=name), latest_sample=sample)
    return CandidateReliability(snapshot, ws, wf, speed)  # type: ignore[arg-type]


def test_the_real_cpu_over_gpu_decision_flips_with_the_speed_term() -> None:
    """Inputs as audited on the live fleet; speeds as measured there (images/s)."""
    pool = [
        _cand("node-02-cpu", 4.1, 73.1, 5.94, 4.07, 480.0),
        _cand("node-03-gpu", 0.0, 641.7, 6.92, 2.95, 2500.0),
    ]
    without = select_best(score_candidates(pool, alpha=1, beta=1, gamma=0.5, wilson_z=1.96))
    with_speed = select_best(
        score_candidates(pool, alpha=1, beta=1, gamma=0.5, delta=1.0, wilson_z=1.96)
    )
    assert without is not None and without.snapshot.node.name == "node-02-cpu"
    assert with_speed is not None and with_speed.snapshot.node.name == "node-03-gpu"
    assert with_speed.c_score == 0.0


def test_unmeasured_is_neutral_and_nothing_measured_is_inert() -> None:
    pool = [_cand("a", 0, 10, 1, 1, 1000.0), _cand("b", 0, 10, 1, 1, None)]
    scored = score_candidates(pool, alpha=1, beta=1, gamma=0.5, delta=1.0, wilson_z=1.96)
    assert [round(c.c_score, 2) for c in scored] == [0.0, 0.5]
    blank = [_cand("a", 0, 10, 1, 1, None), _cand("b", 0, 10, 1, 1, None)]
    scored = score_candidates(blank, alpha=1, beta=1, gamma=0.5, delta=1.0, wilson_z=1.96)
    assert [c.c_score for c in scored] == [0.0, 0.0]


def _measured(session: AsyncSession, node, speed: float, now: datetime) -> None:  # type: ignore[no-untyped-def]
    _add_outcome(session, node, state=LeaseState.COMPLETED, outcome_ts=now - timedelta(minutes=5))
    lease = next(o for o in session.new if isinstance(o, Lease) and o.node_id == node.id)
    lease.samples_per_second = speed


@pytest.mark.asyncio
async def test_speed_is_learned_and_borrowed_within_a_hardware_class(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    cpu = _make_node(session, "node-cpu", with_gpu=False, now=now)
    gpu = _make_node(session, "node-gpu", with_gpu=True, now=now)
    fresh_gpu = _make_node(session, "node-gpu-new", with_gpu=True, now=now)
    for node in (cpu, gpu, fresh_gpu):
        _add_sample(session, node, gpu_util=None, rtt_ewma=20.0, cpu=5.0, now=now)
    _measured(session, cpu, 500.0, now)
    _measured(session, gpu, 2500.0, now)
    job = _placeable_job(session)
    await session.commit()

    decision, cands = await _decide(session, _settings(), job)
    assert decision.delta == 1.0
    assert cands["node-gpu"].raw_samples_per_second == 2500.0
    # Never ran a job here, but it is a GPU machine: presumed GPU-fast.
    assert cands["node-gpu-new"].raw_samples_per_second == 2500.0
    assert cands["node-cpu"].c_score == pytest.approx(0.8)
    assert not cands["node-cpu"].was_selected
