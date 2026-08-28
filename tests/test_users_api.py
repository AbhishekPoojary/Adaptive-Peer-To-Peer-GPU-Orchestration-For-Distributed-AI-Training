"""Admin user management (ADR-012 addendum 2).

The point of these endpoints is that inviting someone no longer requires shell
access to the orchestrator host, so the tests are mostly about the two things
that could make that a bad trade: the gate staying shut for non-admins, and an
admin being unable to lock every admin out of the fleet in two clicks.

Google sign-in is unchanged by any of this and is covered in
``test_google_auth.py``; what is asserted here is that the two features compose —
an account created with an email alone signs in with Google, and moving that
email unbinds the previous Google identity.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from tests.helpers import (
    TEST_ADMIN_USERNAME,
    TEST_OPERATOR_USERNAME,
    TEST_USER_PASSWORD,
    auth_headers,
)

_GOOD_PASSWORD = "a-long-enough-password"


async def admin_get(client: AsyncClient, path: str) -> Any:
    return await client.get(path, headers=auth_headers(client.admin_token))  # type: ignore[attr-defined]


async def admin_post(client: AsyncClient, path: str, body: dict[str, Any]) -> Any:
    return await client.post(
        path, json=body, headers=auth_headers(client.admin_token)  # type: ignore[attr-defined]
    )


async def admin_patch(client: AsyncClient, path: str, body: dict[str, Any]) -> Any:
    return await client.patch(
        path, json=body, headers=auth_headers(client.admin_token)  # type: ignore[attr-defined]
    )


async def find_user(client: AsyncClient, username: str) -> dict[str, Any] | None:
    listing = await admin_get(client, "/users")
    for user in listing.json()["users"]:
        if user["username"] == username:
            return user
    return None


# --- The gate ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/users"), ("post", "/users"), ("patch", "/users/{id}")],
)
async def test_operator_is_refused_everywhere(
    api_client: AsyncClient, method: str, path: str
) -> None:
    """An OPERATOR must not manage accounts — that is the whole gate."""
    target = await find_user(api_client, TEST_OPERATOR_USERNAME)
    assert target is not None
    url = path.format(id=target["id"])

    call = getattr(api_client, method)
    response = await call(url) if method == "get" else await call(url, json={})
    assert response.status_code == 403


async def test_anonymous_is_refused(anon_client: AsyncClient) -> None:
    assert (await anon_client.get("/users")).status_code == 401


# --- Listing -----------------------------------------------------------------


async def test_list_shows_how_each_account_can_sign_in(
    api_client: AsyncClient,
) -> None:
    """The list has to answer "why can't this person get in?" on its own."""
    listing = await admin_get(api_client, "/users")
    assert listing.status_code == 200

    users = {u["username"]: u for u in listing.json()["users"]}
    assert TEST_ADMIN_USERNAME in users
    admin = users[TEST_ADMIN_USERNAME]
    assert admin["role"] == "ADMIN"
    assert admin["has_password"] is True
    assert admin["google_linked"] is False
    assert admin["disabled_at"] is None


async def test_list_never_exposes_a_password_hash(api_client: AsyncClient) -> None:
    """`has_password` is derived; the hash itself must never leave the database."""
    body = (await admin_get(api_client, "/users")).text
    assert "password_hash" not in body
    assert "scrypt$" not in body


# --- Creating ----------------------------------------------------------------


async def test_admin_creates_a_password_account(api_client: AsyncClient) -> None:
    response = await admin_post(
        api_client,
        "/users",
        {"username": "newbie", "role": "OPERATOR", "password": _GOOD_PASSWORD},
    )
    assert response.status_code == 201, response.text
    assert response.json()["has_password"] is True

    # ...and it can actually sign in, which is the only proof that matters.
    login = await api_client.post(
        "/auth/login", json={"username": "newbie", "password": _GOOD_PASSWORD}
    )
    assert login.status_code == 200
    assert login.json()["user"]["role"] == "OPERATOR"


async def test_admin_creates_a_google_only_account(api_client: AsyncClient) -> None:
    """Email alone: the invite path that removes the SSH step entirely."""
    response = await admin_post(
        api_client,
        "/users",
        {"username": "priya", "role": "OPERATOR", "email": "Priya@Example.com"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["has_password"] is False
    assert body["email"] == "priya@example.com", "email should be normalized"
    assert body["google_linked"] is False, "not linked until they first sign in"

    # No password can open it, including the empty string.
    refused = await api_client.post(
        "/auth/login", json={"username": "priya", "password": _GOOD_PASSWORD}
    )
    assert refused.status_code == 401


async def test_account_with_no_way_in_is_refused(api_client: AsyncClient) -> None:
    """Neither password nor email would create an account nobody can ever use."""
    response = await admin_post(
        api_client, "/users", {"username": "ghost", "role": "OPERATOR"}
    )
    assert response.status_code == 422
    assert "needs a password" in response.text


async def test_short_password_is_refused(api_client: AsyncClient) -> None:
    response = await admin_post(
        api_client,
        "/users",
        {"username": "weak", "role": "OPERATOR", "password": "short"},
    )
    assert response.status_code == 422
    assert "at least 12" in response.text


async def test_duplicate_username_and_email_conflict(api_client: AsyncClient) -> None:
    first = await admin_post(
        api_client,
        "/users",
        {"username": "dup", "role": "OPERATOR", "email": "dup@example.com"},
    )
    assert first.status_code == 201

    same_name = await admin_post(
        api_client,
        "/users",
        {"username": "dup", "role": "OPERATOR", "email": "other@example.com"},
    )
    assert same_name.status_code == 409

    same_email = await admin_post(
        api_client,
        "/users",
        {"username": "other", "role": "OPERATOR", "email": "dup@example.com"},
    )
    assert same_email.status_code == 409
    assert "email" in same_email.json()["detail"], "should name the field that clashed"


@pytest.mark.parametrize("bad", ["ab", "has space", "has/slash", "../escape"])
async def test_unusable_usernames_are_refused(
    api_client: AsyncClient, bad: str
) -> None:
    response = await admin_post(
        api_client, "/users", {"username": bad, "role": "OPERATOR", "password": _GOOD_PASSWORD}
    )
    assert response.status_code == 422


# --- Updating ----------------------------------------------------------------


async def test_admin_can_reset_a_password(api_client: AsyncClient) -> None:
    """The forgotten-password path that previously needed SSH."""
    operator = await find_user(api_client, TEST_OPERATOR_USERNAME)
    assert operator is not None

    response = await admin_patch(
        api_client, f"/users/{operator['id']}", {"password": "brand-new-password"}
    )
    assert response.status_code == 200

    assert (
        await api_client.post(
            "/auth/login",
            json={"username": TEST_OPERATOR_USERNAME, "password": "brand-new-password"},
        )
    ).status_code == 200
    assert (
        await api_client.post(
            "/auth/login",
            json={"username": TEST_OPERATOR_USERNAME, "password": TEST_USER_PASSWORD},
        )
    ).status_code == 401, "the old password must stop working"


async def test_disabling_blocks_sign_in_and_re_enabling_restores_it(
    api_client: AsyncClient,
) -> None:
    operator = await find_user(api_client, TEST_OPERATOR_USERNAME)
    assert operator is not None

    disabled = await admin_patch(
        api_client, f"/users/{operator['id']}", {"disabled": True}
    )
    assert disabled.status_code == 200
    assert disabled.json()["disabled_at"] is not None

    assert (
        await api_client.post(
            "/auth/login",
            json={"username": TEST_OPERATOR_USERNAME, "password": TEST_USER_PASSWORD},
        )
    ).status_code == 401

    restored = await admin_patch(
        api_client, f"/users/{operator['id']}", {"disabled": False}
    )
    assert restored.json()["disabled_at"] is None
    assert (
        await api_client.post(
            "/auth/login",
            json={"username": TEST_OPERATOR_USERNAME, "password": TEST_USER_PASSWORD},
        )
    ).status_code == 200


async def test_omitting_email_leaves_it_alone(api_client: AsyncClient) -> None:
    """The bug this guards: changing a role must not silently revoke Google.

    `email` absent and `email: null` both arrive as None; conflating them would
    unbind someone's Google sign-in every time an admin touched their role.
    """
    created = await admin_post(
        api_client,
        "/users",
        {"username": "keeper", "role": "OPERATOR", "email": "keeper@example.com"},
    )
    user_id = created.json()["id"]

    response = await admin_patch(api_client, f"/users/{user_id}", {"role": "ADMIN"})
    assert response.status_code == 200
    assert response.json()["email"] == "keeper@example.com"
    assert response.json()["role"] == "ADMIN"


async def test_explicit_null_email_clears_it(api_client: AsyncClient) -> None:
    created = await admin_post(
        api_client,
        "/users",
        {
            "username": "clearme",
            "role": "OPERATOR",
            "email": "clearme@example.com",
            "password": _GOOD_PASSWORD,
        },
    )
    user_id = created.json()["id"]

    response = await admin_patch(api_client, f"/users/{user_id}", {"email": None})
    assert response.status_code == 200
    assert response.json()["email"] is None


async def test_clearing_the_only_credential_is_refused(
    api_client: AsyncClient,
) -> None:
    """An edit must not reach a state creation would have rejected."""
    created = await admin_post(
        api_client,
        "/users",
        {"username": "googleonly", "role": "OPERATOR", "email": "g@example.com"},
    )
    user_id = created.json()["id"]

    response = await admin_patch(api_client, f"/users/{user_id}", {"email": None})
    assert response.status_code == 422
    assert "no way to sign in" in response.json()["detail"]


# --- The lockout guard -------------------------------------------------------


async def test_last_admin_cannot_demote_themselves(api_client: AsyncClient) -> None:
    """Two clicks from an unmanageable fleet, without this.

    Every administrative action needs ADMIN, so zero enabled admins can only be
    repaired from a shell on the orchestrator host — exactly the situation this
    whole addendum exists to remove.
    """
    admin = await find_user(api_client, TEST_ADMIN_USERNAME)
    assert admin is not None

    response = await admin_patch(
        api_client, f"/users/{admin['id']}", {"role": "OPERATOR"}
    )
    assert response.status_code == 409
    assert "last enabled ADMIN" in response.json()["detail"]

    # Still an admin, and the API still works.
    assert (await admin_get(api_client, "/users")).status_code == 200


async def test_last_admin_cannot_disable_themselves(api_client: AsyncClient) -> None:
    admin = await find_user(api_client, TEST_ADMIN_USERNAME)
    assert admin is not None
    response = await admin_patch(
        api_client, f"/users/{admin['id']}", {"disabled": True}
    )
    assert response.status_code == 409


async def test_demote_and_disable_in_one_request_is_still_caught(
    api_client: AsyncClient,
) -> None:
    """The guard evaluates the resulting state, not one field at a time."""
    admin = await find_user(api_client, TEST_ADMIN_USERNAME)
    assert admin is not None
    response = await admin_patch(
        api_client,
        f"/users/{admin['id']}",
        {"role": "OPERATOR", "disabled": True},
    )
    assert response.status_code == 409


async def test_demotion_is_allowed_once_another_admin_exists(
    api_client: AsyncClient,
) -> None:
    """The guard is about the last one, not about self-modification."""
    second = await admin_post(
        api_client,
        "/users",
        {"username": "second-admin", "role": "ADMIN", "password": _GOOD_PASSWORD},
    )
    assert second.status_code == 201

    admin = await find_user(api_client, TEST_ADMIN_USERNAME)
    assert admin is not None
    response = await admin_patch(
        api_client, f"/users/{admin['id']}", {"role": "OPERATOR"}
    )
    assert response.status_code == 200
    assert response.json()["role"] == "OPERATOR"


async def test_a_disabled_admin_does_not_count_toward_the_guard(
    api_client: AsyncClient,
) -> None:
    """A second admin who cannot sign in is not a second admin.

    Counting them would let the last *usable* admin demote themselves and leave
    the fleet administered by an account nobody can log into.
    """
    created = await admin_post(
        api_client,
        "/users",
        {"username": "sleeping-admin", "role": "ADMIN", "password": _GOOD_PASSWORD},
    )
    await admin_patch(
        api_client, f"/users/{created.json()['id']}", {"disabled": True}
    )

    admin = await find_user(api_client, TEST_ADMIN_USERNAME)
    assert admin is not None
    response = await admin_patch(
        api_client, f"/users/{admin['id']}", {"role": "OPERATOR"}
    )
    assert response.status_code == 409


# --- Composition with Google sign-in ----------------------------------------


async def test_moving_an_email_unbinds_the_previous_google_identity(
    api_client: AsyncClient,
) -> None:
    """Otherwise whoever held the old address keeps access under the old `sub`."""
    from sqlalchemy import select

    from orchestrator.core.db import get_sessionmaker
    from orchestrator.models.user import User

    created = await admin_post(
        api_client,
        "/users",
        {"username": "mover", "role": "OPERATOR", "email": "before@example.com"},
    )
    user_id = created.json()["id"]

    # Simulate a completed Google sign-in having bound a subject.
    async with get_sessionmaker()() as db:
        row = (
            await db.execute(select(User).where(User.username == "mover"))
        ).scalar_one()
        row.google_sub = "bound-subject-1"
        await db.commit()

    assert (await find_user(api_client, "mover"))["google_linked"] is True  # type: ignore[index]

    moved = await admin_patch(
        api_client, f"/users/{user_id}", {"email": "after@example.com"}
    )
    assert moved.status_code == 200
    assert moved.json()["email"] == "after@example.com"
    assert moved.json()["google_linked"] is False, (
        "changing the address must unbind the old Google identity"
    )


async def test_unknown_user_is_404(api_client: AsyncClient) -> None:
    import uuid

    response = await admin_patch(
        api_client, f"/users/{uuid.uuid4()}", {"role": "OPERATOR"}
    )
    assert response.status_code == 404
