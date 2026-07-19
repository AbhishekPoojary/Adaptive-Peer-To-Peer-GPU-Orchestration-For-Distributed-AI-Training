"""Concurrency test for single-use enrollment-token semantics.

N agents present the same token to POST /nodes/register at once. Exactly one
must succeed; the rest must be rejected (409 already-used, or 401). This runs
against real Postgres because the guarantee under test is Postgres row-locking
under READ COMMITTED — SQLite would not exercise it.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient

from tests.helpers import generate_node_keypair, mint_enrollment_token, sample_hardware

_CONCURRENCY = 8


@pytest.mark.asyncio
async def test_single_token_yields_exactly_one_registration(
    api_client: AsyncClient,
) -> None:
    token = await mint_enrollment_token(api_client, created_by="race-test")

    async def attempt() -> int:
        _, pub = generate_node_keypair()
        resp = await api_client.post(
            "/nodes/register",
            json={
                "enrollment_token": token,
                "public_key": pub,
                "hardware": sample_hardware(with_gpu=False),
                "agent_version": "test-0.1",
            },
        )
        return resp.status_code

    statuses = await asyncio.gather(*(attempt() for _ in range(_CONCURRENCY)))

    successes = [s for s in statuses if s == 201]
    losers = [s for s in statuses if s != 201]

    assert len(successes) == 1, f"expected exactly one success, got {statuses}"
    assert all(s in (401, 409) for s in losers), f"unexpected loser codes: {statuses}"
    assert len(losers) == _CONCURRENCY - 1
