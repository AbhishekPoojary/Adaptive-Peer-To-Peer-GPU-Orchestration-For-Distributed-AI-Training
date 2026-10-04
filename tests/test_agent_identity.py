"""A machine keeps one identity across restarts, and recovers when it is refused.

demo.ps1 used to wipe the agent's saved identity on every run, because after a
database reset the agent would cling to a node the orchestrator no longer knew.
Every run therefore enrolled the same laptop as a new machine, leaving the old
one in the fleet list and throwing away its reliability history. The agent now
handles the refusal itself, so the identity can be kept.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent.main import (
    AgentState,
    _establish_identity,
    _generate_keypair,
    _private_key_to_pem,
    load_state,
    save_state,
)


def _saved(state_dir: Path) -> AgentState:
    key, _pem = _generate_keypair()
    state = AgentState(
        node_id="old-node", name="node-130", private_key_pem=_private_key_to_pem(key)
    )
    save_state(state_dir, state)
    return state


def _orchestrator(*, knows_old_node: bool, reachable: bool = True) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if not reachable:
            raise httpx.ConnectError("orchestrator down", request=request)
        path = request.url.path
        body: dict[str, Any] = json.loads(request.content or b"{}")
        if path == "/auth/challenge":
            if body["node_id"] == "old-node" and not knows_old_node:
                return httpx.Response(404, json={"detail": "unknown node"})
            return httpx.Response(200, json={"nonce": "n", "expires_at": "2099-01-01T00:00:00Z"})
        if path == "/auth/token/refresh":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 900})
        if path == "/nodes/register":
            return httpx.Response(
                201,
                json={"node_id": "new-node", "name": "node-131", "access_token": "tok2",
                      "expires_in": 900},
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def _establish(
    tmp_path: Path, transport: httpx.MockTransport, token: str | None
) -> AgentState:
    async with httpx.AsyncClient(transport=transport) as client:
        state, _key, _access, _expiry = await _establish_identity(
            client, orchestrator="http://o", state_dir=tmp_path, enrollment_token=token
        )
    return state


@pytest.mark.asyncio
async def test_a_known_identity_resumes_and_ignores_the_token(tmp_path: Path) -> None:
    """The common case: same machine, same node, history intact -- even though
    the launcher passes a fresh token every run."""
    _saved(tmp_path)
    state = await _establish(tmp_path, _orchestrator(knows_old_node=True), token="fresh")
    assert state.name == "node-130"


@pytest.mark.asyncio
async def test_a_refused_identity_is_replaced_using_the_token(tmp_path: Path) -> None:
    """Removed from the fleet, or the database was reset: enrol again."""
    _saved(tmp_path)
    state = await _establish(tmp_path, _orchestrator(knows_old_node=False), token="fresh")
    assert state.name == "node-131"
    saved = load_state(tmp_path)
    assert saved is not None and saved.node_id == "new-node"


@pytest.mark.asyncio
async def test_a_refused_identity_without_a_token_stops(tmp_path: Path) -> None:
    _saved(tmp_path)
    with pytest.raises(SystemExit):
        await _establish(tmp_path, _orchestrator(knows_old_node=False), token=None)
    assert load_state(tmp_path).node_id == "old-node"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_an_unreachable_orchestrator_never_costs_the_identity(tmp_path: Path) -> None:
    """A network failure is not a refusal: re-enrolling on it would create a
    duplicate machine every time the Wi-Fi dropped at start-up."""
    _saved(tmp_path)
    with pytest.raises(SystemExit):
        await _establish(
            tmp_path, _orchestrator(knows_old_node=True, reachable=False), token="fresh"
        )
    assert load_state(tmp_path).node_id == "old-node"  # type: ignore[union-attr]
