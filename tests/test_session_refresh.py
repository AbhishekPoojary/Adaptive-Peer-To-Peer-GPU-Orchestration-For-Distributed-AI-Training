"""POST /auth/refresh keeps an open dashboard signed in, within limits.

A user token lives 15 minutes and nothing renewed it, so the dashboard sent
people back to the sign-in page mid-use. The dashboard now swaps a still-valid
token for a fresh one; these tests pin down what that swap may and may not do.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.core.security import create_user_jwt
from orchestrator.models.user import UserRole
from tests.helpers import TEST_JWT_KEY, auth_headers, seed_user


def _claims(token: str) -> dict[str, int]:
    return jwt.decode(token, TEST_JWT_KEY, algorithms=["HS256"], audience="user")


@pytest.mark.asyncio
async def test_refresh_returns_a_working_token_that_keeps_the_sign_in_time(
    anon_client: AsyncClient,
) -> None:
    old = anon_client.operator_token  # type: ignore[attr-defined]
    resp = await anon_client.post("/auth/refresh", headers=auth_headers(old))
    assert resp.status_code == 200, resp.text
    new = resp.json()["access_token"]

    me = await anon_client.get("/auth/me", headers=auth_headers(new))
    assert me.status_code == 200
    assert _claims(new)["auth_time"] == _claims(old)["auth_time"]
    assert _claims(new)["exp"] >= _claims(old)["exp"]


@pytest.mark.asyncio
async def test_refresh_stops_after_the_session_cap(
    anon_client: AsyncClient, session: AsyncSession
) -> None:
    async with session as db:
        user = await seed_user(db, username="long-session", role=UserRole.OPERATOR)
    signed_in_13h_ago = int(time.time()) - 13 * 3600
    token = create_user_jwt(
        user_id=str(user.id), username=user.username, role=user.role.value,
        signing_key=TEST_JWT_KEY, ttl_seconds=900, session_started_at=signed_in_13h_ago,
    )
    resp = await anon_client.post("/auth/refresh", headers=auth_headers(token))
    assert resp.status_code == 401
    assert "sign in again" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_a_disabled_account_cannot_refresh(
    anon_client: AsyncClient, session: AsyncSession
) -> None:
    async with session as db:
        user = await seed_user(db, username="switched-off", role=UserRole.OPERATOR)
        token = create_user_jwt(
            user_id=str(user.id), username=user.username, role=user.role.value,
            signing_key=TEST_JWT_KEY, ttl_seconds=900,
        )
        user.disabled_at = datetime.now(UTC)
        await db.commit()
    resp = await anon_client.post("/auth/refresh", headers=auth_headers(token))
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_needs_a_token(anon_client: AsyncClient) -> None:
    assert (await anon_client.post("/auth/refresh")).status_code == 401
