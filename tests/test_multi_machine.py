"""One job across several machines: who can be paired, and where they meet.

Machines at different sites cannot accept each other's connections, so a
multi-machine cohort is only formed from machines behind one router (same
public address), and every rank is told rank 0's LAN address. The LAN address
comes from the peer and is handed to other peers as MASTER_ADDR, so it must
parse as an IP address.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.models.node import Node
from orchestrator.services.scheduling import _same_network
from tests.helpers import auth_headers, register_new_node


def _snap(name: str, public: str | None) -> SimpleNamespace:
    return SimpleNamespace(node=SimpleNamespace(name=name, public_address=public))


def test_a_cohort_is_the_first_group_on_one_network() -> None:
    ordered = [_snap("a", "1.1.1.1"), _snap("b", "2.2.2.2"), _snap("c", None),
               _snap("d", "2.2.2.2"), _snap("e", "1.1.1.1")]
    picked = _same_network(ordered, 2)  # type: ignore[arg-type]
    assert [s.node.name for s in picked] == ["b", "d"]  # type: ignore[union-attr]
    assert _same_network(ordered, 3) is None  # type: ignore[arg-type]
    assert [s.node.name for s in _same_network(ordered, 1)] == ["a"]  # type: ignore[arg-type,union-attr]


def _beat(lan: object) -> dict[str, object]:
    return {"cpu_percent": 5.0, "ram_used_bytes": 1, "ram_total_bytes": 2,
            "gpu": None, "rtt_ms": None, "lan_address": lan}


@pytest.mark.asyncio
async def test_heartbeat_records_where_the_machine_is(
    api_client: AsyncClient, session: AsyncSession
) -> None:
    reg, _key = await register_new_node(api_client)
    url, headers = f"/nodes/{reg['node_id']}/heartbeat", auth_headers(reg["access_token"])

    bad = await api_client.post(url, json=_beat("10.0.0.5; rm -rf /"), headers=headers)
    assert bad.status_code == 422  # never stored, never handed to another peer

    ok = await api_client.post(url, json=_beat("192.168.1.20"), headers=headers)
    assert ok.status_code == 200, ok.text
    async with session as db:
        node = (await db.execute(select(Node).where(Node.name == reg["name"]))).scalar_one()
    assert node.lan_address == "192.168.1.20"
    assert node.public_address  # the address the heartbeat arrived from
