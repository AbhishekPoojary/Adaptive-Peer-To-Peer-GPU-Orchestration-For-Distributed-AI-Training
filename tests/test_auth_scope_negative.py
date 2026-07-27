"""Negative authorization tests for the M8 boundaries (ADR-012).

These are the tests that decide whether the user layer is real or decorative.
Node tokens and user tokens are both HS256 JWTs signed with the *same* key, so
the `aud` claim is the only thing separating a peer machine from a human
operator. If that check were missing, every enrolled node — i.e. every
classmate's laptop — would already hold a valid operator credential, and every
gate added in M8 would be bypassable by a token the system hands out freely.

So this file asserts the boundary holds in *both* directions, and that every
human-facing route actually rejects an anonymous caller rather than a
representative sample of them.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.db import get_sessionmaker
from orchestrator.core.security import create_node_jwt, create_user_jwt
from orchestrator.models.user import User
from tests.helpers import (
    TEST_JWT_KEY,
    TEST_OPERATOR_USERNAME,
    auth_headers,
    generate_node_keypair,
    mint_enrollment_token,
    register_new_node,
    sample_hardware,
    schedule_single_rank_job,
    send_heartbeat,
)

_SPEC = {
    "dataset": "mnist",
    "model": "cnn",
    "epochs": 1,
    "batch_size": 8,
    "learning_rate": 0.1,
    "world_size": 1,
    "min_gpu_mem_bytes": None,
}


def _human_routes(job_id: str, node_id: str) -> list[tuple[str, str]]:
    """Every route that requires a human operator, as (method, path)."""
    return [
        ("POST", "/jobs"),
        ("GET", "/jobs"),
        ("GET", f"/jobs/{job_id}"),
        ("GET", f"/jobs/{job_id}/metrics"),
        ("GET", f"/jobs/{job_id}/logs"),
        ("GET", f"/jobs/{job_id}/scheduling-decisions"),
        ("POST", f"/jobs/{job_id}/cancel"),
        ("GET", "/nodes"),
        ("GET", f"/nodes/{node_id}"),
        ("GET", "/auth/me"),
    ]


async def _call(client: AsyncClient, method: str, path: str, **kw: object) -> object:
    if method == "POST":
        body = {"spec": _SPEC} if path == "/jobs" else {}
        return await client.post(path, json=body, **kw)  # type: ignore[arg-type]
    return await client.get(path, **kw)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_every_human_route_rejects_anonymous(anon_client: AsyncClient) -> None:
    """No human-facing route is reachable without a credential.

    Enumerated exhaustively rather than spot-checked: before M8 this whole
    surface was open, and the way that happened was nobody checking all of it.
    """
    reg, _key = await register_new_node(anon_client)
    for method, path in _human_routes(str(uuid.uuid4()), reg["node_id"]):
        resp = await _call(anon_client, method, path)
        assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_node_token_cannot_act_as_a_human(anon_client: AsyncClient) -> None:
    """A node's own JWT must not open the operator surface.

    This is the attack that matters: every peer legitimately holds one of these.
    """
    reg, _key = await register_new_node(anon_client)
    headers = auth_headers(reg["access_token"])

    for method, path in _human_routes(str(uuid.uuid4()), reg["node_id"]):
        resp = await _call(anon_client, method, path, headers=headers)
        assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_user_token_cannot_act_as_a_node(anon_client: AsyncClient) -> None:
    """A user JWT must not reach node-scoped endpoints.

    The reverse direction. An operator token is not a licence to heartbeat as
    a machine or claim leases on its behalf.
    """
    reg, _key = await register_new_node(anon_client)
    node_id = reg["node_id"]
    headers = auth_headers(anon_client.operator_token)  # type: ignore[attr-defined]

    resp = await anon_client.post(
        f"/nodes/{node_id}/heartbeat",
        json={
            "cpu_percent": 1.0,
            "ram_used_bytes": 1,
            "ram_total_bytes": 2,
            "gpu": None,
            "rtt_ms": None,
        },
        headers=headers,
    )
    assert resp.status_code == 401, resp.text

    resp = await anon_client.post(f"/nodes/{node_id}/leases/claim", headers=headers)
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_admin_token_forged_by_flipping_the_role_claim_is_rejected(
    anon_client: AsyncClient,
) -> None:
    """Self-asserting ADMIN in the token is not enough; the row decides.

    ``require_admin_user`` re-reads the live user, so a token minted with
    ``role="ADMIN"`` for an account that is really an OPERATOR gets 403. This
    also pins the behaviour that a role *downgrade* takes effect immediately
    rather than at the next token expiry.
    """
    me = await anon_client.get(
        "/auth/me",
        headers=auth_headers(anon_client.operator_token),  # type: ignore[attr-defined]
    )
    assert me.status_code == 200
    operator = me.json()
    assert operator["role"] == "OPERATOR"

    forged = create_user_jwt(
        user_id=operator["id"],
        username=operator["username"],
        role="ADMIN",  # the lie
        signing_key=TEST_JWT_KEY,
        ttl_seconds=900,
    )
    resp = await anon_client.post(
        "/auth/enrollment-tokens",
        json={"created_by": "forged"},
        headers=auth_headers(forged),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_node_token_for_a_deleted_node_is_rejected_on_human_routes(
    anon_client: AsyncClient,
) -> None:
    """A syntactically valid JWT for a nonexistent subject fails closed.

    Guards against a decoder that returns a subject but no lookup verifying it
    exists — which would make an unenrolled attacker's token as good as a real
    one on any route that only checks the signature.
    """
    ghost = create_node_jwt(
        node_id=str(uuid.uuid4()), signing_key=TEST_JWT_KEY, ttl_seconds=900
    )
    resp = await anon_client.get("/nodes", headers=auth_headers(ghost))
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_job_attribution_comes_from_the_token_not_the_body(
    api_client: AsyncClient,
) -> None:
    """``submitted_by`` is the authenticated username (ADR-012 §4).

    And a client still sending the old field gets a loud 422 rather than
    having its claimed identity quietly ignored.
    """
    resp = await api_client.post("/jobs", json={"spec": _SPEC})
    assert resp.status_code == 201, resp.text
    assert resp.json()["submitted_by"] == "pytest-operator"

    spoofed = await api_client.post(
        "/jobs", json={"spec": _SPEC, "submitted_by": "somebody-else"}
    )
    assert spoofed.status_code == 422, spoofed.text


@pytest.mark.asyncio
async def test_disabled_user_token_stops_working_immediately(
    anon_client: AsyncClient,
) -> None:
    """Disabling an account invalidates its outstanding tokens at once.

    The user row is re-read on every request precisely so revocation does not
    have to wait out the token TTL.
    """
    headers = auth_headers(anon_client.operator_token)  # type: ignore[attr-defined]
    assert (await anon_client.get("/auth/me", headers=headers)).status_code == 200

    maker = get_sessionmaker()
    async with maker() as session:
        await session.execute(
            update(User)
            .where(User.username == TEST_OPERATOR_USERNAME)
            .values(disabled_at=func.now())
        )
        await session.commit()

    resp = await anon_client.get("/auth/me", headers=headers)
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_a_node_can_still_do_its_whole_job_without_a_user_token(
    anon_client: AsyncClient, session: AsyncSession
) -> None:
    """Gating the human surface must not have broken the agent.

    The agent used to read the training spec from ``GET /jobs/{id}``, which is
    now human-only — a node token is (correctly) rejected there. If the spec
    did not travel with the claim instead, every agent would claim work and
    then immediately fail it, and nothing in the human-facing tests would
    notice because they never run an agent.

    So: prove a node holding only its own token can claim work *and* get the
    spec it needs to launch, and separately that the old route is shut.
    """
    reg, _key = await register_new_node(anon_client, with_gpu=True)
    node_id = uuid.UUID(reg["node_id"])
    headers = auth_headers(reg["access_token"])

    await send_heartbeat(
        anon_client, node_id=str(node_id), token=reg["access_token"], gpu_util=5.0
    )
    job = schedule_single_rank_job(session, node_id=node_id, spec=dict(_SPEC))
    await session.commit()

    claim = await anon_client.post(
        f"/nodes/{node_id}/leases/claim", headers=headers
    )
    assert claim.status_code == 200, claim.text
    body = claim.json()
    assert body["lease"] is not None, "the node must still be able to claim work"
    assert body["job_spec"] == _SPEC, "the spec must travel with the grant"

    # And the route it used to use is shut to it.
    old_route = await anon_client.get(f"/jobs/{job.id}", headers=headers)
    assert old_route.status_code == 401


@pytest.mark.asyncio
async def test_enrollment_remains_open_to_valid_tokens(
    anon_client: AsyncClient,
) -> None:
    """M8 must not have locked nodes out of enrolling.

    ``POST /nodes/register`` is authenticated by the one-time enrollment token
    in the body, not a JWT — a node has no JWT yet. A regression that put user
    auth on this route would make the fleet unjoinable, so it is pinned.
    """
    token = await mint_enrollment_token(anon_client)
    _key, pub = generate_node_keypair()
    resp = await anon_client.post(
        "/nodes/register",
        json={
            "enrollment_token": token,
            "public_key": pub,
            "hardware": sample_hardware(with_gpu=False),
            "agent_version": "test-0.1",
        },
    )
    assert resp.status_code == 201, resp.text
