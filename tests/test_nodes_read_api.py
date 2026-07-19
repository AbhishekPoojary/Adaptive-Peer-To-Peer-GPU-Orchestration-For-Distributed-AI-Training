"""Tests for the read-only fleet views: GET /nodes and GET /nodes/{id}.

Runs against a real Postgres (see tests/conftest.py); telemetry-sample
fixtures inserted directly via asyncpg are explicitly synthetic request/DB
fixture data (not telemetry claimed to be real hardware output), matching the
convention already used in test_auth_flow.py's null-gpu round-trip test.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from httpx import AsyncClient

from orchestrator.core.config import get_settings
from tests.helpers import asyncpg_dsn, register_new_node


@pytest.mark.asyncio
async def test_list_nodes_empty(api_client: AsyncClient) -> None:
    resp = await api_client.get("/nodes")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"nodes": []}


@pytest.mark.asyncio
async def test_list_nodes_after_register_and_heartbeat(api_client: AsyncClient) -> None:
    body, _ = await register_new_node(api_client, with_gpu=True)
    node_id = body["node_id"]
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    payload = {
        "cpu_percent": 42.0,
        "ram_used_bytes": 4 * 1024**3,
        "ram_total_bytes": 16 * 1024**3,
        "gpu": [
            {
                "util_percent": 12.0,
                "mem_used_bytes": 1 * 1024**3,
                "mem_total_bytes": 8 * 1024**3,
                "temperature_c": 60.0,
                "power_w": 45.0,
            }
        ],
        "rtt_ms": None,
    }
    hb = await api_client.post(f"/nodes/{node_id}/heartbeat", json=payload, headers=headers)
    assert hb.status_code == 200, hb.text

    resp = await api_client.get("/nodes")
    assert resp.status_code == 200, resp.text
    nodes = resp.json()["nodes"]
    assert len(nodes) == 1
    node = nodes[0]

    assert node["id"] == node_id
    assert node["name"] == body["name"]
    assert node["status"] == "ONLINE"
    assert node["last_heartbeat_at"] is not None
    assert node["heartbeat_stale"] is False
    assert node["lease_success_count"] == 0
    assert node["lease_failure_count"] == 0

    hardware = node["hardware"]
    assert hardware["hostname"] == "pytest-fixture-host"
    assert len(hardware["gpus"]) == 1

    latest = node["latest_telemetry"]
    assert latest is not None
    assert latest["cpu_percent"] == 42.0
    assert latest["ram_used_bytes"] == 4 * 1024**3
    assert latest["gpu"][0]["util_percent"] == 12.0
    # First-ever heartbeat sent rtt_ms=None -> no EWMA seeded yet.
    assert latest["rtt_ms"] is None
    assert latest["rtt_ewma_ms"] is None


@pytest.mark.asyncio
async def test_list_nodes_no_heartbeat_yet_is_stale_with_null_telemetry(
    api_client: AsyncClient,
) -> None:
    """A freshly registered node that has never heartbeated is trivially
    stale and reports no telemetry — never a fabricated sample."""
    await register_new_node(api_client)

    resp = await api_client.get("/nodes")
    assert resp.status_code == 200, resp.text
    node = resp.json()["nodes"][0]
    assert node["last_heartbeat_at"] is None
    assert node["heartbeat_stale"] is True
    assert node["latest_telemetry"] is None


@pytest.mark.asyncio
async def test_heartbeat_stale_flips_true_after_window_without_mutating_status(
    api_client: AsyncClient,
) -> None:
    """heartbeat_stale is computed at read time from the staleness window and
    never mutates the stored node status (that is the failure detector's
    job, ADR-004 / M6) — status stays ONLINE even once stale."""
    body, _ = await register_new_node(api_client)
    node_id = body["node_id"]
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    hb = await api_client.post(
        f"/nodes/{node_id}/heartbeat",
        json={"cpu_percent": 1.0, "ram_used_bytes": 1, "ram_total_bytes": 2},
        headers=headers,
    )
    assert hb.status_code == 200, hb.text

    # Freshly heartbeated: not stale yet.
    fresh = await api_client.get("/nodes")
    assert fresh.json()["nodes"][0]["heartbeat_stale"] is False

    # Push last_heartbeat_at into the past, well beyond the staleness window,
    # directly in the DB (no real time.sleep — deterministic and fast).
    conn = await asyncpg.connect(asyncpg_dsn(os.environ["DATABASE_URL"]))
    try:
        stale_stamp = datetime.now(UTC) - timedelta(
            seconds=get_settings().heartbeat_stale_seconds + 5.0
        )
        await conn.execute(
            "UPDATE nodes SET last_heartbeat_at = $1 WHERE id = $2",
            stale_stamp,
            uuid.UUID(node_id),
        )
    finally:
        await conn.close()

    stale = await api_client.get("/nodes")
    node = stale.json()["nodes"][0]
    assert node["heartbeat_stale"] is True
    # Status was never mutated by the read path.
    assert node["status"] == "ONLINE"


@pytest.mark.asyncio
async def test_node_detail_404_for_unknown_node(api_client: AsyncClient) -> None:
    resp = await api_client.get(f"/nodes/{uuid.uuid4()}")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_node_detail_sample_cap_and_ordering(api_client: AsyncClient) -> None:
    """GET /nodes/{id}?samples=N returns at most N samples, newest first."""
    body, _ = await register_new_node(api_client)
    node_id = body["node_id"]
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    for cpu_percent in (10.0, 20.0, 30.0, 40.0, 50.0):
        hb = await api_client.post(
            f"/nodes/{node_id}/heartbeat",
            json={
                "cpu_percent": cpu_percent,
                "ram_used_bytes": 1,
                "ram_total_bytes": 2,
            },
            headers=headers,
        )
        assert hb.status_code == 200, hb.text

    resp = await api_client.get(f"/nodes/{node_id}", params={"samples": 2})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    samples = data["telemetry_samples"]
    assert len(samples) == 2
    # Newest first: the last two heartbeats sent, in reverse chronological order.
    assert [s["cpu_percent"] for s in samples] == [50.0, 40.0]
    # The detail view's own summary fields mirror the list view's.
    assert data["latest_telemetry"]["cpu_percent"] == 50.0


@pytest.mark.asyncio
async def test_node_detail_samples_capped_by_configured_max(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the caller asks for more, the server-configured max wins."""
    monkeypatch.setenv("NODE_DETAIL_MAX_SAMPLES", "3")
    get_settings.cache_clear()

    body, _ = await register_new_node(api_client)
    node_id = body["node_id"]
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    for i in range(5):
        hb = await api_client.post(
            f"/nodes/{node_id}/heartbeat",
            json={"cpu_percent": float(i), "ram_used_bytes": 1, "ram_total_bytes": 2},
            headers=headers,
        )
        assert hb.status_code == 200, hb.text

    resp = await api_client.get(f"/nodes/{node_id}", params={"samples": 1000})
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["telemetry_samples"]) == 3

    get_settings.cache_clear()
