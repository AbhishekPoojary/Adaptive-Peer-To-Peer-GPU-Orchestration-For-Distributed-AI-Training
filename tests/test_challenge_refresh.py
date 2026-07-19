"""Challenge-response JWT refresh tests (ADR-008, stage 3).

A node proves possession of its Ed25519 private key by signing a server-issued
nonce. Valid signature -> new JWT; wrong key -> 401; nonce reuse -> 401.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.helpers import generate_node_keypair, register_new_node, sign_b64


async def _get_nonce(client: AsyncClient, node_id: str) -> str:
    resp = await client.post("/auth/challenge", json={"node_id": node_id})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["nonce"])


@pytest.mark.asyncio
async def test_valid_signature_mints_new_jwt(api_client: AsyncClient) -> None:
    body, private_key = await register_new_node(api_client)
    node_id = str(body["node_id"])
    nonce = await _get_nonce(api_client, node_id)

    resp = await api_client.post(
        "/auth/token/refresh",
        json={
            "node_id": node_id,
            "nonce": nonce,
            "signature": sign_b64(private_key, nonce.encode()),
        },
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]

    # The freshly minted token must actually authenticate a heartbeat.
    hb = await api_client.post(
        f"/nodes/{node_id}/heartbeat",
        json={"cpu_percent": 1.0, "ram_used_bytes": 1, "ram_total_bytes": 2},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert hb.status_code == 200, hb.text


@pytest.mark.asyncio
async def test_wrong_key_signature_rejected(api_client: AsyncClient) -> None:
    body, _ = await register_new_node(api_client)
    node_id = str(body["node_id"])
    nonce = await _get_nonce(api_client, node_id)

    # Sign with an unrelated key the node never registered.
    attacker_key, _ = generate_node_keypair()
    resp = await api_client.post(
        "/auth/token/refresh",
        json={
            "node_id": node_id,
            "nonce": nonce,
            "signature": sign_b64(attacker_key, nonce.encode()),
        },
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_nonce_is_single_use(api_client: AsyncClient) -> None:
    body, private_key = await register_new_node(api_client)
    node_id = str(body["node_id"])
    nonce = await _get_nonce(api_client, node_id)
    signature = sign_b64(private_key, nonce.encode())

    first = await api_client.post(
        "/auth/token/refresh",
        json={"node_id": node_id, "nonce": nonce, "signature": signature},
    )
    assert first.status_code == 200, first.text

    # Replaying the same nonce+signature must fail: the nonce was consumed.
    second = await api_client.post(
        "/auth/token/refresh",
        json={"node_id": node_id, "nonce": nonce, "signature": signature},
    )
    assert second.status_code == 401, second.text


@pytest.mark.asyncio
async def test_challenge_for_unknown_node_404(api_client: AsyncClient) -> None:
    import uuid

    resp = await api_client.post(
        "/auth/challenge", json={"node_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 404, resp.text
