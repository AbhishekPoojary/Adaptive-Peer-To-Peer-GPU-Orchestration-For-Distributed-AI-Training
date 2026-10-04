"""An admin can remove a machine from the fleet (DELETE /nodes/{id}).

Every machine that ever enrolled used to stay in the fleet list for good, so a
long-lived deployment's Overview read "0 / N" against a denominator of dead
laptops. Removal hides the node and shuts out its agent, but keeps its row:
leases, audits and jobs all name it.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import (
    auth_headers,
    register_new_node,
    schedule_single_rank_job,
    send_heartbeat,
)


def _admin(client: AsyncClient) -> dict[str, str]:
    return auth_headers(client.admin_token)  # type: ignore[attr-defined]


async def _listed(client: AsyncClient) -> set[str]:
    return {n["id"] for n in (await client.get("/nodes")).json()["nodes"]}


@pytest.mark.asyncio
async def test_admin_removes_an_idle_node_and_it_is_shut_out(api_client: AsyncClient) -> None:
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id, token = reg["node_id"], reg["access_token"]
    await send_heartbeat(api_client, node_id=node_id, token=token, gpu_util=5.0)
    assert node_id in await _listed(api_client)

    resp = await api_client.delete(f"/nodes/{node_id}", headers=_admin(api_client))
    assert resp.status_code == 204, resp.text
    assert node_id not in await _listed(api_client)

    # Its still-unexpired token no longer lets it back in...
    beat = await api_client.post(
        f"/nodes/{node_id}/heartbeat",
        json={"cpu_percent": 1, "ram_used_bytes": 1, "ram_total_bytes": 2},
        headers=auth_headers(token),
    )
    assert beat.status_code == 401
    # ...and it cannot mint itself a fresh one.
    challenge = await api_client.post("/auth/challenge", json={"node_id": node_id})
    assert challenge.status_code == 404

    # Idempotent.
    again = await api_client.delete(f"/nodes/{node_id}", headers=_admin(api_client))
    assert again.status_code == 204


@pytest.mark.asyncio
async def test_only_admins_remove_nodes(api_client: AsyncClient) -> None:
    reg, _key = await register_new_node(api_client, with_gpu=True)
    resp = await api_client.delete(f"/nodes/{reg['node_id']}")  # operator token
    assert resp.status_code == 403
    assert reg["node_id"] in await _listed(api_client)


@pytest.mark.asyncio
async def test_a_node_with_live_work_is_not_removed(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    reg, _key = await register_new_node(api_client, with_gpu=True)
    node_id = reg["node_id"]
    await send_heartbeat(api_client, node_id=node_id, token=reg["access_token"], gpu_util=5.0)
    schedule_single_rank_job(session, node_id=uuid.UUID(node_id))
    await session.commit()

    resp = await api_client.delete(f"/nodes/{node_id}", headers=_admin(api_client))
    assert resp.status_code == 409
    assert node_id in await _listed(api_client)


@pytest.mark.asyncio
async def test_unknown_node_is_404(api_client: AsyncClient) -> None:
    resp = await api_client.delete(f"/nodes/{uuid.uuid4()}", headers=_admin(api_client))
    assert resp.status_code == 404
