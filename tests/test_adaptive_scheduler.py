"""Black-box property tests for the adaptive scheduler (M3, ADR-009).

These are the milestone gate. They run against a real Postgres (the ``session``
fixture), not mocked math: realistic node/telemetry/lease-history rows are
inserted, a real scheduler pass places an ``adaptive`` job, and the persisted
``SchedulingDecision`` audit rows are read back and asserted. The fixtures use
synthetic-but-realistic inputs (allowed for tests per CONTRIBUTING.md #5); the
scoring under test is entirely production code.

Properties pinned:
  * a node that just failed a lease loses rank to a clean-history twin;
  * a saturated node loses rank to an idle twin;
  * a high-latency node loses rank to a low-latency twin;
  * reliability recovers over time (an old failure is penalised less than a
    fresh one of the same magnitude);
  * a no-telemetry node is never preferred over any node with real telemetry;
  * the audit log is complete and its numbers match hand computation.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.config import Settings
from orchestrator.models.job import Job, JobState
from orchestrator.models.lease import Lease, LeaseState
from orchestrator.models.node import Node, NodeStatus, NodeTelemetrySample
from orchestrator.schemas.scheduling import (
    SchedulingCandidateOut,
    SchedulingDecisionOut,
)
from orchestrator.services.scheduling import (
    list_scheduling_decisions,
    run_scheduler_pass,
)
from tests.helpers import register_new_node, send_heartbeat

_GB = 1024**3


# --- Direct-insert fixtures (real rows, synthetic inputs) --------------------


def _make_node(
    session: AsyncSession,
    name: str,
    *,
    with_gpu: bool = True,
    heartbeat_age_s: float = 1.0,
    now: datetime,
) -> Node:
    gpus = (
        [{"name": "T", "vram_bytes": 8 * _GB, "driver_version": "x"}] if with_gpu else []
    )
    node = Node(
        id=uuid.uuid4(),
        name=name,
        public_key="test-pem",
        status=NodeStatus.ONLINE,
        last_heartbeat_at=now - timedelta(seconds=heartbeat_age_s),
        hardware={"hostname": name, "os": "t", "cpu_model": "t", "cores": 4,
                  "ram_bytes": 16 * _GB, "gpus": gpus},
        agent_version="t",
        reliability_prior_alpha=1.0,
        reliability_prior_beta=1.0,
    )
    session.add(node)
    return node


def _add_sample(
    session: AsyncSession,
    node: Node,
    *,
    gpu_util: float | None,
    rtt_ewma: float | None,
    cpu: float = 5.0,
    now: datetime,
) -> None:
    gpu = (
        [{"util_percent": gpu_util, "mem_used_bytes": 0, "mem_total_bytes": _GB,
          "temperature_c": None, "power_w": None}]
        if gpu_util is not None
        else None
    )
    session.add(
        NodeTelemetrySample(
            node_id=node.id, ts=now, cpu_percent=cpu, ram_used_bytes=0,
            ram_total_bytes=_GB, gpu=gpu, rtt_ms=None, rtt_ewma_ms=rtt_ewma,
        )
    )


def _add_outcome(
    session: AsyncSession, node: Node, *, state: LeaseState, outcome_ts: datetime
) -> None:
    """Insert one terminal lease (with the job it belonged to) as reliability
    history. COMPLETED = success; FAILED/EXPIRED = failure. The job is left in a
    terminal state so it is never re-picked by the pass under test.
    """
    job_state = JobState.COMPLETED if state is LeaseState.COMPLETED else JobState.FAILED
    job = Job(
        id=uuid.uuid4(),
        spec={"dataset": "mnist", "model": "m", "epochs": 1, "batch_size": 8,
              "learning_rate": 0.1, "world_size": 1, "min_gpu_mem_bytes": None},
        scheduler_name="adaptive",
        state=job_state,
        submitted_by="history-fixture",
    )
    session.add(job)
    # EXPIRED leases carry no released_at (the sweep only sets state); the
    # reliability query then falls back to expires_at — mirror that here.
    released_at = None if state is LeaseState.EXPIRED else outcome_ts
    session.add(
        Lease(
            id=uuid.uuid4(),
            job_id=job.id,
            node_id=node.id,
            lease_epoch=1,
            state=state,
            granted_at=outcome_ts - timedelta(seconds=30),
            expires_at=outcome_ts,
            released_at=released_at,
        )
    )


def _placeable_job(session: AsyncSession) -> Job:
    job = Job(
        id=uuid.uuid4(),
        spec={"dataset": "mnist", "model": "m", "epochs": 1, "batch_size": 8,
              "learning_rate": 0.1, "world_size": 1, "min_gpu_mem_bytes": None},
        scheduler_name="adaptive",
        state=JobState.QUEUED,
        submitted_by="adaptive-test",
    )
    session.add(job)
    return job


def _settings(**overrides: object) -> Settings:
    """Settings for a pass. Defaults give alpha=1, beta=1, gamma=0.5, z=1.96,
    prior 1/1; override reliability_decay_halflife_seconds per test."""
    base: dict[str, object] = {
        "scheduler_alpha_load": 1.0,
        "scheduler_beta_reliability": 1.0,
        "scheduler_gamma_latency": 0.5,
        "reliability_wilson_z": 1.96,
        "heartbeat_stale_seconds": 15.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def _decide(
    session: AsyncSession, settings: Settings, job: Job
) -> tuple[SchedulingDecisionOut, dict[str, SchedulingCandidateOut]]:
    """Run one pass, commit, and return the single decision plus its candidates
    keyed by node name."""
    await run_scheduler_pass(session, settings=settings)
    await session.commit()
    decisions = await list_scheduling_decisions(session, job_id=job.id)
    assert len(decisions) == 1, f"expected exactly one decision, got {len(decisions)}"
    decision = decisions[0]
    return decision, {c.node_name: c for c in decision.candidates}


# --- Property: a recent failure loses rank -----------------------------------


@pytest.mark.asyncio
async def test_recent_failure_loses_rank_vs_clean_history(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    clean = _make_node(session, "node-clean", now=now)
    failed = _make_node(session, "node-failed", now=now)
    # Identical telemetry so only reliability differs.
    _add_sample(session, clean, gpu_util=40.0, rtt_ewma=25.0, now=now)
    _add_sample(session, failed, gpu_util=40.0, rtt_ewma=25.0, now=now)
    _add_outcome(session, failed, state=LeaseState.FAILED, outcome_ts=now)
    job = _placeable_job(session)
    await session.commit()

    _, cands = await _decide(session, _settings(reliability_decay_halflife_seconds=86400.0), job)
    clean_c, failed_c = cands["node-clean"], cands["node-failed"]
    # Failure lowers reliability -> raises S (worse rank), never better.
    assert clean_c.r_score > failed_c.r_score
    assert failed_c.s_score > clean_c.s_score
    assert clean_c.was_selected and not failed_c.was_selected


# --- Property: a saturated node loses rank -----------------------------------


@pytest.mark.asyncio
async def test_saturated_node_loses_rank_vs_idle(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    idle = _make_node(session, "node-idle", now=now)
    busy = _make_node(session, "node-busy", now=now)
    _add_sample(session, idle, gpu_util=10.0, rtt_ewma=25.0, now=now)
    _add_sample(session, busy, gpu_util=90.0, rtt_ewma=25.0, now=now)
    job = _placeable_job(session)
    await session.commit()

    _, cands = await _decide(session, _settings(reliability_decay_halflife_seconds=86400.0), job)
    idle_c, busy_c = cands["node-idle"], cands["node-busy"]
    assert busy_c.l_score > idle_c.l_score
    assert busy_c.s_score > idle_c.s_score
    assert idle_c.was_selected and not busy_c.was_selected


# --- Property: a high-latency node loses rank --------------------------------


@pytest.mark.asyncio
async def test_high_latency_node_loses_rank_vs_low_latency(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    low = _make_node(session, "node-low", now=now)
    high = _make_node(session, "node-high", now=now)
    _add_sample(session, low, gpu_util=40.0, rtt_ewma=10.0, now=now)
    _add_sample(session, high, gpu_util=40.0, rtt_ewma=200.0, now=now)
    job = _placeable_job(session)
    await session.commit()

    _, cands = await _decide(session, _settings(reliability_decay_halflife_seconds=86400.0), job)
    low_c, high_c = cands["node-low"], cands["node-high"]
    assert high_c.d_score > low_c.d_score
    assert high_c.s_score > low_c.s_score
    assert low_c.was_selected and not high_c.was_selected


# --- Property: reliability recovers over time --------------------------------


@pytest.mark.asyncio
async def test_reliability_recovers_over_time(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    half_life = 3600.0
    old = _make_node(session, "node-old-fail", now=now)
    recent = _make_node(session, "node-recent-fail", now=now)
    _add_sample(session, old, gpu_util=40.0, rtt_ewma=25.0, now=now)
    _add_sample(session, recent, gpu_util=40.0, rtt_ewma=25.0, now=now)
    # Same-magnitude failure; the old one is 10 half-lives in the past.
    _add_outcome(session, old, state=LeaseState.FAILED,
                 outcome_ts=now - timedelta(seconds=10 * half_life))
    _add_outcome(session, recent, state=LeaseState.FAILED, outcome_ts=now)
    job = _placeable_job(session)
    await session.commit()

    _, cands = await _decide(session, _settings(reliability_decay_halflife_seconds=half_life), job)
    old_c, recent_c = cands["node-old-fail"], cands["node-recent-fail"]
    # The decayed old failure weighs almost nothing: its penalty is smaller, so
    # reliability is higher and S lower than the fresh failure's.
    assert old_c.weighted_failure < recent_c.weighted_failure
    assert old_c.r_score > recent_c.r_score
    assert old_c.s_score < recent_c.s_score
    assert old_c.was_selected and not recent_c.was_selected


# --- Property: a no-telemetry node is never preferred ------------------------


@pytest.mark.asyncio
async def test_no_telemetry_node_never_preferred(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    # Two nodes with real telemetry and one ONLINE-but-never-sampled node.
    good = _make_node(session, "node-good", now=now)
    okay = _make_node(session, "node-okay", now=now)
    # A fresh-heartbeat node that has never reported a telemetry sample.
    _make_node(session, "node-blind", now=now)
    _add_sample(session, good, gpu_util=20.0, rtt_ewma=20.0, now=now)
    _add_sample(session, okay, gpu_util=80.0, rtt_ewma=80.0, now=now)
    job = _placeable_job(session)
    await session.commit()

    _, cands = await _decide(session, _settings(reliability_decay_halflife_seconds=86400.0), job)
    blind_c = cands["node-blind"]
    # Worst-case load and latency, honestly stored as unknown raw inputs.
    assert blind_c.l_score == 1.0 and blind_c.d_score == 1.0
    assert blind_c.raw_util is None and blind_c.raw_rtt_ewma_ms is None
    assert not blind_c.was_selected
    # Never strictly preferred over ANY node with real telemetry.
    assert blind_c.s_score >= cands["node-good"].s_score
    assert blind_c.s_score >= cands["node-okay"].s_score
    # The winner has real telemetry.
    assert cands["node-good"].was_selected
    assert cands["node-good"].raw_util is not None


# --- Property: audit-log completeness + hand-computed numbers ----------------


def _wilson(s: float, f: float, z: float = 1.96) -> float:
    n = s + f
    p = s / n
    z2 = z * z
    center = p + z2 / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)
    return (center - margin) / (1 + z2 / n)


@pytest.mark.asyncio
async def test_audit_log_completeness_and_hand_computed_numbers(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    # A near-infinite half-life makes every decay weight ~= 1, so weighted
    # counts are (prior + integer outcomes) and the numbers are hand-checkable.
    settings = _settings(reliability_decay_halflife_seconds=1e12)

    a = _make_node(session, "node-a", now=now)
    b = _make_node(session, "node-b", now=now)
    c = _make_node(session, "node-c", now=now)
    _add_sample(session, a, gpu_util=10.0, rtt_ewma=20.0, now=now)
    _add_sample(session, b, gpu_util=50.0, rtt_ewma=60.0, now=now)
    _add_sample(session, c, gpu_util=90.0, rtt_ewma=100.0, now=now)
    # a: 2 successes; b: 1 success + 1 failure; c: no history (prior only).
    _add_outcome(session, a, state=LeaseState.COMPLETED, outcome_ts=now)
    _add_outcome(session, a, state=LeaseState.COMPLETED, outcome_ts=now)
    _add_outcome(session, b, state=LeaseState.COMPLETED, outcome_ts=now)
    _add_outcome(session, b, state=LeaseState.EXPIRED, outcome_ts=now)
    job = _placeable_job(session)
    await session.commit()

    decision, cands = await _decide(session, settings, job)

    # Completeness: one row per eligible candidate, weights captured.
    assert len(decision.candidates) == 3
    assert {c.node_name for c in decision.candidates} == {"node-a", "node-b", "node-c"}
    assert (decision.alpha, decision.beta, decision.gamma) == (1.0, 1.0, 0.5)
    assert decision.wilson_z == 1.96

    a_c, b_c, c_c = cands["node-a"], cands["node-b"], cands["node-c"]

    # Weighted pseudo-counts (prior 1/1 + decayed ~= 1 each).
    assert math.isclose(a_c.weighted_success, 3.0, abs_tol=1e-4)
    assert math.isclose(a_c.weighted_failure, 1.0, abs_tol=1e-4)
    assert math.isclose(b_c.weighted_success, 2.0, abs_tol=1e-4)
    assert math.isclose(b_c.weighted_failure, 2.0, abs_tol=1e-4)
    assert math.isclose(c_c.weighted_success, 1.0, abs_tol=1e-4)
    assert math.isclose(c_c.weighted_failure, 1.0, abs_tol=1e-4)

    # L min-max over util [10,50,90]: 0, 0.5, 1.0.
    assert math.isclose(a_c.l_score, 0.0, abs_tol=1e-9)
    assert math.isclose(b_c.l_score, 0.5, abs_tol=1e-9)
    assert math.isclose(c_c.l_score, 1.0, abs_tol=1e-9)
    # D min-max over rtt [20,60,100]: 0, 0.5, 1.0.
    assert math.isclose(a_c.d_score, 0.0, abs_tol=1e-9)
    assert math.isclose(b_c.d_score, 0.5, abs_tol=1e-9)
    assert math.isclose(c_c.d_score, 1.0, abs_tol=1e-9)

    # R = Wilson lower bound of the weighted counts.
    assert math.isclose(a_c.r_score, _wilson(3, 1), abs_tol=1e-4)
    assert math.isclose(b_c.r_score, _wilson(2, 2), abs_tol=1e-4)
    assert math.isclose(c_c.r_score, _wilson(1, 1), abs_tol=1e-4)

    # S = 1*L - 1*R + 0.5*D, hand-assembled.
    assert math.isclose(a_c.s_score, 0.0 - _wilson(3, 1) + 0.5 * 0.0, abs_tol=1e-4)
    assert math.isclose(b_c.s_score, 0.5 - _wilson(2, 2) + 0.5 * 0.5, abs_tol=1e-4)
    assert math.isclose(c_c.s_score, 1.0 - _wilson(1, 1) + 0.5 * 1.0, abs_tol=1e-4)

    # node-a has the lowest S -> selected; exactly one winner.
    assert a_c.was_selected
    assert [c.was_selected for c in decision.candidates].count(True) == 1
    assert decision.selected_node_id == a.id


# --- End-to-end over HTTP: registry + submit + audit endpoint ----------------


@pytest.mark.asyncio
async def test_adaptive_end_to_end_over_http(api_client: AsyncClient) -> None:
    # Two real registered nodes heartbeating with different GPU load; the
    # submit-triggered adaptive pass must place on the idle one and expose the
    # decision via the read endpoint.
    reg_busy, _k1 = await register_new_node(api_client, with_gpu=True)
    reg_idle, _k2 = await register_new_node(api_client, with_gpu=True)
    await send_heartbeat(
        api_client, node_id=reg_busy["node_id"], token=reg_busy["access_token"], gpu_util=95.0
    )
    await send_heartbeat(
        api_client, node_id=reg_idle["node_id"], token=reg_idle["access_token"], gpu_util=5.0
    )

    submit = await api_client.post(
        "/jobs",
        json={
            "spec": {"dataset": "mnist", "model": "m", "epochs": 1, "batch_size": 8,
                     "learning_rate": 0.1, "world_size": 1, "min_gpu_mem_bytes": 4 * _GB},
            "scheduler_name": "adaptive",
        },
    )
    assert submit.status_code == 201
    body = submit.json()
    assert body["state"] == "SCHEDULED"
    assert body["scheduled_node_id"] == reg_idle["node_id"]

    decisions = (
        await api_client.get(f"/jobs/{body['id']}/scheduling-decisions")
    ).json()["decisions"]
    assert len(decisions) == 1
    cands = {c["node_id"]: c for c in decisions[0]["candidates"]}
    assert len(cands) == 2
    assert cands[reg_idle["node_id"]]["was_selected"] is True
    assert cands[reg_busy["node_id"]]["was_selected"] is False
    # Lower load -> lower L -> lower S for the idle node.
    assert cands[reg_idle["node_id"]]["s_score"] < cands[reg_busy["node_id"]]["s_score"]
    assert decisions[0]["scheduler_name"] == "adaptive"


@pytest.mark.asyncio
async def test_scheduling_decisions_endpoint_404_for_unknown_job(
    api_client: AsyncClient,
) -> None:
    resp = await api_client.get(f"/jobs/{uuid.uuid4()}/scheduling-decisions")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_baseline_scheduler_writes_no_decision(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    # least_loaded is not audited: the decisions endpoint stays empty for it.
    reg, _k = await register_new_node(api_client, with_gpu=True)
    await send_heartbeat(
        api_client, node_id=reg["node_id"], token=reg["access_token"], gpu_util=5.0
    )
    submit = await api_client.post(
        "/jobs",
        json={
            "spec": {"dataset": "mnist", "model": "m", "epochs": 1, "batch_size": 8,
                     "learning_rate": 0.1, "world_size": 1, "min_gpu_mem_bytes": None},
            "scheduler_name": "least_loaded",
        },
    )
    job_id = submit.json()["id"]
    decisions = (await api_client.get(f"/jobs/{job_id}/scheduling-decisions")).json()
    assert decisions["decisions"] == []
