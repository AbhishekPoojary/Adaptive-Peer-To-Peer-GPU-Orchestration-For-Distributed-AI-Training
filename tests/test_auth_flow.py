"""End-to-end auth + heartbeat tests against a real Postgres.

Covers the happy path (mint token -> register -> authenticated heartbeat) plus
the security boundaries: expired token, expired JWT, cross-node scoping, and
telemetry validation.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from httpx import AsyncClient

from orchestrator.core.security import create_node_jwt, hash_token
from tests.helpers import (
    TEST_ADMIN_KEY,
    TEST_JWT_KEY,
    asyncpg_dsn,
    auth_headers,
    generate_node_keypair,
    mint_enrollment_token,
    register_new_node,
    sample_hardware,
)

_JWT_KEY = TEST_JWT_KEY  # the key the test app is configured with (conftest)


@pytest.mark.asyncio
async def test_full_enrollment_and_heartbeat(api_client: AsyncClient) -> None:
    """Mint a token, register a node, and post an authenticated heartbeat."""
    body, _ = await register_new_node(api_client, with_gpu=True)
    node_id = body["node_id"]
    assert body["name"].startswith("node-")
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0

    headers = {"Authorization": f"Bearer {body['access_token']}"}
    payload = {
        "cpu_percent": 12.5,
        "ram_used_bytes": 4 * 1024**3,
        "ram_total_bytes": 16 * 1024**3,
        "gpu": [
            {
                "util_percent": 30.0,
                "mem_used_bytes": 2 * 1024**3,
                "mem_total_bytes": 8 * 1024**3,
                "temperature_c": 55.0,
                "power_w": 120.0,
            }
        ],
        "rtt_ms": 8.0,
    }
    resp = await api_client.post(f"/nodes/{node_id}/heartbeat", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ONLINE"
    assert "server_time" in data
    # First measured RTT seeds the EWMA exactly (no prior to blend with).
    assert data["rtt_ewma_ms"] == 8.0


@pytest.mark.asyncio
async def test_heartbeat_null_gpu_roundtrips_as_null(api_client: AsyncClient) -> None:
    """A CPU node reporting no GPU telemetry must persist SQL NULL, not zeros."""
    body, _ = await register_new_node(api_client, with_gpu=False)
    node_id = str(body["node_id"])
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    payload = {
        "cpu_percent": 5.0,
        "ram_used_bytes": 1 * 1024**3,
        "ram_total_bytes": 8 * 1024**3,
        "gpu": None,
        "rtt_ms": None,
    }
    resp = await api_client.post(f"/nodes/{node_id}/heartbeat", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["rtt_ewma_ms"] is None

    # Verify the stored row is genuinely NULL (not 0 / not "[]").
    conn = await asyncpg.connect(asyncpg_dsn(os.environ["DATABASE_URL"]))
    try:
        row = await conn.fetchrow(
            "SELECT gpu, rtt_ms, rtt_ewma_ms FROM node_telemetry_samples "
            "WHERE node_id = $1",
            uuid.UUID(node_id),
        )
    finally:
        await conn.close()
    assert row["gpu"] is None
    assert row["rtt_ms"] is None
    assert row["rtt_ewma_ms"] is None


@pytest.mark.asyncio
async def test_expired_enrollment_token_rejected(api_client: AsyncClient) -> None:
    """A token whose expiry is in the past must not enroll a node (401)."""
    # Insert an already-expired token directly (the API forbids ttl<=0).
    raw = "expired-token-" + uuid.uuid4().hex
    conn = await asyncpg.connect(asyncpg_dsn(os.environ["DATABASE_URL"]))
    try:
        await conn.execute(
            "INSERT INTO enrollment_tokens (id, token_hash, created_at, expires_at, created_by) "
            "VALUES ($1, $2, now(), $3, $4)",
            uuid.uuid4(),
            hash_token(raw),
            datetime.now(UTC) - timedelta(seconds=10),
            "pytest-expired",
        )
    finally:
        await conn.close()

    _, pub = generate_node_keypair()
    resp = await api_client.post(
        "/nodes/register",
        json={
            "enrollment_token": raw,
            "public_key": pub,
            "hardware": sample_hardware(with_gpu=False),
            "agent_version": "test-0.1",
        },
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_expired_jwt_rejected_on_heartbeat(api_client: AsyncClient) -> None:
    """An expired but otherwise valid JWT must be rejected (401)."""
    body, _ = await register_new_node(api_client)
    node_id = str(body["node_id"])
    stale = create_node_jwt(
        node_id=node_id,
        signing_key=_JWT_KEY,
        ttl_seconds=1,
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    resp = await api_client.post(
        f"/nodes/{node_id}/heartbeat",
        json={"cpu_percent": 1.0, "ram_used_bytes": 1, "ram_total_bytes": 2},
        headers={"Authorization": f"Bearer {stale}"},
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_node_a_token_rejected_on_node_b(api_client: AsyncClient) -> None:
    """Node A's valid JWT must be forbidden (403) on node B's heartbeat path."""
    body_a, _ = await register_new_node(api_client)
    body_b, _ = await register_new_node(api_client)
    headers_a = {"Authorization": f"Bearer {body_a['access_token']}"}

    resp = await api_client.post(
        f"/nodes/{body_b['node_id']}/heartbeat",
        json={"cpu_percent": 1.0, "ram_used_bytes": 1, "ram_total_bytes": 2},
        headers=headers_a,
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_missing_bearer_rejected(api_client: AsyncClient) -> None:
    body, _ = await register_new_node(api_client)
    resp = await api_client.post(
        f"/nodes/{body['node_id']}/heartbeat",
        json={"cpu_percent": 1.0, "ram_used_bytes": 1, "ram_total_bytes": 2},
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_payload",
    [
        {"cpu_percent": 150.0, "ram_used_bytes": 1, "ram_total_bytes": 2},  # util>100
        {"cpu_percent": 10.0, "ram_used_bytes": -1, "ram_total_bytes": 2},  # negative RAM
        {"cpu_percent": 10.0, "ram_used_bytes": 5, "ram_total_bytes": 2},  # used>total
        {"cpu_percent": 10.0, "ram_used_bytes": 1, "ram_total_bytes": 2, "extra": 1},  # extra field
        {
            "cpu_percent": 10.0,
            "ram_used_bytes": 1,
            "ram_total_bytes": 2,
            "gpu": [{"util_percent": 200.0, "mem_used_bytes": 1, "mem_total_bytes": 2}],
        },  # gpu util>100
    ],
)
async def test_malformed_telemetry_rejected(
    api_client: AsyncClient, bad_payload: dict
) -> None:
    """Impossible telemetry values are rejected at the schema boundary (422)."""
    body, _ = await register_new_node(api_client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    resp = await api_client.post(
        f"/nodes/{body['node_id']}/heartbeat", json=bad_payload, headers=headers
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_token_minting_requires_admin_credentials(
    anon_client: AsyncClient,
) -> None:
    """Minting accepts the static admin key **or** an ADMIN user token, and
    nothing else (ADR-012 §6).

    Run against ``anon_client`` so each case carries exactly the credential
    under test — the default ``api_client`` would silently add an operator
    token and turn the "no credentials" case into the "wrong role" case.
    """
    body = {"created_by": "x"}

    # No credentials at all.
    resp = await anon_client.post("/auth/enrollment-tokens", json=body)
    assert resp.status_code == 401, resp.text

    # A wrong admin key is rejected without falling through to user auth.
    resp = await anon_client.post(
        "/auth/enrollment-tokens", json=body, headers={"X-Admin-Key": "wrong"}
    )
    assert resp.status_code == 401, resp.text

    # A genuine, authenticated user who simply lacks the role: 403, not 401.
    # The distinction matters — 401 would tell them to log in again, which
    # would not help.
    resp = await anon_client.post(
        "/auth/enrollment-tokens",
        json=body,
        headers=auth_headers(anon_client.operator_token),  # type: ignore[attr-defined]
    )
    assert resp.status_code == 403, resp.text

    # The static admin key still works: CLI bootstrap before any user exists.
    resp = await anon_client.post(
        "/auth/enrollment-tokens", json=body, headers={"X-Admin-Key": TEST_ADMIN_KEY}
    )
    assert resp.status_code == 201, resp.text

    # And so does an ADMIN user token — the path the dashboard uses now that
    # it no longer holds the admin key at all.
    resp = await anon_client.post(
        "/auth/enrollment-tokens",
        json=body,
        headers=auth_headers(anon_client.admin_token),  # type: ignore[attr-defined]
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_register_rejects_non_ed25519_key(api_client: AsyncClient) -> None:
    """A public key that is not a valid Ed25519 PEM is rejected (400)."""
    token = await mint_enrollment_token(api_client)
    resp = await api_client.post(
        "/nodes/register",
        json={
            "enrollment_token": token,
            "public_key": "-----BEGIN PUBLIC KEY-----\nnot-a-key\n-----END PUBLIC KEY-----\n",
            "hardware": sample_hardware(with_gpu=False),
            "agent_version": "test-0.1",
        },
    )
    assert resp.status_code == 400, resp.text
