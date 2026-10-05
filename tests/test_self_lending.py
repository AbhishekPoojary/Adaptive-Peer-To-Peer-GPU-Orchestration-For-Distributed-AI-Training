"""Anyone signed in can lend their own computer ("Lend my computer").

Adding a machine used to need an admin to mint a join command and send it over.
Now any signed-in user may mint one for themselves while ``ALLOW_SELF_LENDING``
is on. The token is stamped with their username -- never a label from the
request -- and the machine that uses it records them as its owner
(``Node.enrolled_by``), which is what lets them see it and remove it again.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from orchestrator.core.config import get_settings
from tests.helpers import (
    TEST_ADMIN_USERNAME,
    TEST_OPERATOR_USERNAME,
    auth_headers,
    generate_node_keypair,
    register_new_node,
    sample_hardware,
)


def _operator(client: AsyncClient) -> dict[str, str]:
    return auth_headers(client.operator_token)  # type: ignore[attr-defined]


def _admin(client: AsyncClient) -> dict[str, str]:
    return auth_headers(client.admin_token)  # type: ignore[attr-defined]


async def _enroll_with(client: AsyncClient, headers: dict[str, str]) -> str:
    """Mint a join command as the given user and enroll a machine with it."""
    minted = await client.post(
        "/auth/enrollment-tokens", json={"created_by": "spoofed-label"}, headers=headers
    )
    assert minted.status_code == 201, minted.text
    _key, pub = generate_node_keypair()
    reg = await client.post(
        "/nodes/register",
        json={
            "enrollment_token": minted.json()["token"],
            "public_key": pub,
            "hardware": sample_hardware(with_gpu=True),
            "agent_version": "test-0.1",
        },
    )
    assert reg.status_code in (200, 201), reg.text
    return str(reg.json()["node_id"])


async def _node(client: AsyncClient, node_id: str) -> dict[str, object] | None:
    nodes = (await client.get("/nodes", headers=_operator(client))).json()["nodes"]
    return next((n for n in nodes if n["id"] == node_id), None)


@pytest.mark.asyncio
async def test_an_operator_lends_their_own_computer(anon_client: AsyncClient) -> None:
    node_id = await _enroll_with(anon_client, _operator(anon_client))

    node = await _node(anon_client, node_id)
    assert node is not None
    # The owner is the signed-in user, not whatever the request body claimed.
    assert node["enrolled_by"] == TEST_OPERATOR_USERNAME

    tokens = (
        await anon_client.get("/auth/enrollment-tokens", headers=_admin(anon_client))
    ).json()["tokens"]
    assert {t["created_by"] for t in tokens} == {TEST_OPERATOR_USERNAME}


@pytest.mark.asyncio
async def test_a_lender_removes_their_own_computer_but_no_one_elses(
    anon_client: AsyncClient,
) -> None:
    mine = await _enroll_with(anon_client, _operator(anon_client))
    admins = await _enroll_with(anon_client, _admin(anon_client))
    assert (await _node(anon_client, admins))["enrolled_by"] == TEST_ADMIN_USERNAME  # type: ignore[index]

    other = await anon_client.delete(f"/nodes/{admins}", headers=_operator(anon_client))
    assert other.status_code == 403
    assert await _node(anon_client, admins) is not None

    own = await anon_client.delete(f"/nodes/{mine}", headers=_operator(anon_client))
    assert own.status_code == 204, own.text
    assert await _node(anon_client, mine) is None

    # Removed is gone: a second attempt is a 404, not a second success.
    again = await anon_client.delete(f"/nodes/{mine}", headers=_operator(anon_client))
    assert again.status_code == 404


@pytest.mark.asyncio
async def test_an_admin_still_removes_anyones_computer(anon_client: AsyncClient) -> None:
    lent = await _enroll_with(anon_client, _operator(anon_client))
    resp = await anon_client.delete(f"/nodes/{lent}", headers=_admin(anon_client))
    assert resp.status_code == 204
    assert await _node(anon_client, lent) is None


@pytest.mark.asyncio
async def test_lending_can_be_limited_to_admins(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_SELF_LENDING", "false")
    get_settings.cache_clear()

    refused = await anon_client.post(
        "/auth/enrollment-tokens", json={"created_by": "x"}, headers=_operator(anon_client)
    )
    assert refused.status_code == 403
    assert "admin" in refused.json()["detail"]

    allowed = await anon_client.post(
        "/auth/enrollment-tokens", json={"created_by": "x"}, headers=_admin(anon_client)
    )
    assert allowed.status_code == 201

    providers = (await anon_client.get("/auth/providers")).json()
    assert providers["lending"] is False


@pytest.mark.asyncio
async def test_providers_report_lending_open_by_default(anon_client: AsyncClient) -> None:
    assert (await anon_client.get("/auth/providers")).json()["lending"] is True


@pytest.mark.asyncio
async def test_admin_key_tokens_keep_their_label_and_belong_to_no_user(
    api_client: AsyncClient,
) -> None:
    """CLI bootstrap has no user: the machine records the label, which matches
    no account, so only an admin can remove it."""
    reg, _key = await register_new_node(api_client)
    node = await _node(api_client, reg["node_id"])
    assert node is not None and node["enrolled_by"] == "pytest"
