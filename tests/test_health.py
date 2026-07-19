"""Tests for GET /health.

No fake data: the "db down" case is produced by pointing DATABASE_URL at
a real (but unreachable/nonexistent) host and letting the real asyncpg
driver fail — never by monkeypatching the health check to return a
canned value.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_degrades_when_db_unreachable(
    monkeypatch: pytest.MonkeyPatch, app_client: AsyncClient
) -> None:
    """With no real Postgres reachable, /health must report db down and a
    503, not a fabricated "ok"."""
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://orchestrator:orchestrator@127.0.0.1:1/orchestrator",
    )

    response = await app_client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["db"] == "down"
    assert body["status"] == "degraded"


@pytest.mark.asyncio
async def test_health_reports_ok_against_real_database(
    monkeypatch: pytest.MonkeyPatch, app_client: AsyncClient
) -> None:
    """When DATABASE_URL points at a real reachable Postgres, /health must
    report db ok. Skipped when no such database is available (e.g. plain
    `pytest -q` without the compose stack running) — we never fabricate
    the "ok" result to make this pass."""
    import os

    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL not set; requires a live Postgres (see deploy/compose.yaml)")

    monkeypatch.setenv("DATABASE_URL", database_url)

    response = await app_client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["db"] == "ok"
    assert body["status"] == "ok"
