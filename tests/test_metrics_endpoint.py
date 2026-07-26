"""Tests for GET /metrics (M7): Prometheus exposition wired to real events.

Asserts the exposition format is well-formed and that counters/gauges reflect
real actions taken through the real API in this test (a heartbeat, a node
registration) — never a hardcoded or simulated value.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.helpers import register_new_node, send_heartbeat


def _metric_value(body: str, metric_line_prefix: str) -> float | None:
    """Parse the value of the first exposition line starting with the given
    metric name (with or without a label set) from the raw text body."""
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        if line.startswith(metric_line_prefix):
            return float(line.rsplit(" ", 1)[-1])
    return None


@pytest.mark.asyncio
async def test_metrics_is_prometheus_text_format(api_client: AsyncClient) -> None:
    resp = await api_client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text
    # Every declared series appears, even with zero real events so far.
    assert "orchestrator_heartbeats_received_total" in body
    assert "orchestrator_scheduling_decisions_total" in body
    assert "orchestrator_jobs_placed_total" in body
    assert "orchestrator_failure_detections_total" in body
    assert "orchestrator_lease_expiries_total" in body
    assert "orchestrator_nodes" in body
    assert "orchestrator_jobs" in body
    assert "orchestrator_leases_active" in body


@pytest.mark.asyncio
async def test_metrics_nodes_gauge_reflects_real_registration(
    api_client: AsyncClient,
) -> None:
    before = await api_client.get("/metrics")
    online_before = _metric_value(before.text, 'orchestrator_nodes{status="ONLINE"}') or 0.0

    reg, _key = await register_new_node(api_client, with_gpu=True)
    await send_heartbeat(api_client, node_id=reg["node_id"], token=reg["access_token"])

    after = await api_client.get("/metrics")
    online_after = _metric_value(after.text, 'orchestrator_nodes{status="ONLINE"}')
    assert online_after == online_before + 1.0


@pytest.mark.asyncio
async def test_metrics_heartbeats_counter_increments_on_real_heartbeat(
    api_client: AsyncClient,
) -> None:
    reg, _key = await register_new_node(api_client, with_gpu=False)

    before = await api_client.get("/metrics")
    count_before = _metric_value(before.text, "orchestrator_heartbeats_received_total") or 0.0

    await send_heartbeat(api_client, node_id=reg["node_id"], token=reg["access_token"])

    after = await api_client.get("/metrics")
    count_after = _metric_value(after.text, "orchestrator_heartbeats_received_total")
    assert count_after == count_before + 1.0


@pytest.mark.asyncio
async def test_metrics_jobs_gauge_has_zero_for_unused_states(api_client: AsyncClient) -> None:
    """A job state with no current rows still reports a real zero, not a
    silently-missing series."""
    resp = await api_client.get("/metrics")
    body = resp.text
    assert _metric_value(body, 'orchestrator_jobs{state="CANCELLED"}') == 0.0
    # Sanity: an unrelated random uuid never appears anywhere in the exposition.
    assert str(uuid.uuid4()) not in body
