"""Anyone with the dashboard link can create their own account (POST /auth/register).

Every account used to be made by an admin, so a friend sent the link could not
get in. A self-registered account is always an OPERATOR -- it uploads its own
data and runs jobs, and sees nothing of anyone else's (ADR-012 addendum 3).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from orchestrator.core.config import get_settings
from tests.helpers import auth_headers

_PASSWORD = "a-long-enough-password"


@pytest.mark.asyncio
async def test_register_signs_you_straight_in_as_an_operator(anon_client: AsyncClient) -> None:
    response = await anon_client.post(
        "/auth/register", json={"username": "new-friend", "password": _PASSWORD}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user"]["username"] == "new-friend"
    assert body["user"]["role"] == "OPERATOR"

    me = await anon_client.get("/auth/me", headers=auth_headers(body["access_token"]))
    assert me.status_code == 200
    # And the password works for an ordinary sign-in afterwards.
    again = await anon_client.post(
        "/auth/login", json={"username": "new-friend", "password": _PASSWORD}
    )
    assert again.status_code == 200


@pytest.mark.asyncio
async def test_you_cannot_make_yourself_an_admin(anon_client: AsyncClient) -> None:
    response = await anon_client.post(
        "/auth/register",
        json={"username": "sneaky", "password": _PASSWORD, "role": "ADMIN"},
    )
    assert response.status_code == 422  # unknown field refused outright


@pytest.mark.asyncio
async def test_taken_username_is_409(anon_client: AsyncClient) -> None:
    body = {"username": "twice", "password": _PASSWORD}
    assert (await anon_client.post("/auth/register", json=body)).status_code == 201
    assert (await anon_client.post("/auth/register", json=body)).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"username": "shortpw", "password": "short"},
        {"username": "a", "password": _PASSWORD},
        {"username": "has space", "password": _PASSWORD},
    ],
)
async def test_unusable_credentials_are_refused(
    anon_client: AsyncClient, body: dict[str, str]
) -> None:
    assert (await anon_client.post("/auth/register", json=body)).status_code == 422


@pytest.mark.asyncio
async def test_registration_can_be_closed(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "allow_registration", False)
    response = await anon_client.post(
        "/auth/register", json={"username": "too-late", "password": _PASSWORD}
    )
    assert response.status_code == 403
    providers = await anon_client.get("/auth/providers")
    assert providers.json()["registration"] is False


@pytest.mark.asyncio
async def test_providers_tells_the_sign_in_page_it_may_offer_sign_up(
    anon_client: AsyncClient,
) -> None:
    providers = await anon_client.get("/auth/providers")
    assert providers.json()["registration"] is True


@pytest.mark.asyncio
async def test_providers_names_the_origins_google_will_accept(
    anon_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So the sign-in page can hide a Google button that could only fail -- on a
    quick-tunnel address, which can never be registered with Google."""
    settings = get_settings()
    monkeypatch.setattr(settings, "google_oauth_client_id", "abc.apps.googleusercontent.com")
    monkeypatch.setattr(
        settings, "google_oauth_origins", " http://localhost:5173/ ,http://127.0.0.1:5173"
    )
    google = (await anon_client.get("/auth/providers")).json()["google"]
    assert google["enabled"] is True
    assert google["origins"] == ["http://localhost:5173", "http://127.0.0.1:5173"]
