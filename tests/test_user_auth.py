"""Password hashing, login, and the enrollment-token admin surface (ADR-012)."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from orchestrator.core.security import hash_password, verify_password
from orchestrator.models.user import UserRole
from orchestrator.services.users import (
    MIN_PASSWORD_LENGTH,
    UserExistsError,
    WeakPasswordError,
    create_user,
)
from tests.helpers import (
    TEST_ADMIN_KEY,
    TEST_OPERATOR_USERNAME,
    TEST_USER_PASSWORD,
    auth_headers,
    generate_node_keypair,
    login,
    mint_enrollment_token,
    sample_hardware,
)

# --- Password hashing --------------------------------------------------------


def test_password_round_trips() -> None:
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored) is True
    assert verify_password("Correct horse battery staple", stored) is False


def test_hash_is_salted_so_equal_passwords_differ() -> None:
    """Two accounts with the same password must not share a hash — otherwise
    the database itself reveals which users chose the same password."""
    a = hash_password("same-password-here")
    b = hash_password("same-password-here")
    assert a != b
    assert verify_password("same-password-here", a)
    assert verify_password("same-password-here", b)


def test_hash_records_its_own_cost_parameters() -> None:
    """The format carries n/r/p so the cost can be raised later without
    invalidating rows written under the old parameters."""
    scheme, n, r, p, salt, digest = hash_password("whatever-goes-here").split("$")
    assert scheme == "scrypt"
    assert (int(n), int(r), int(p)) == (2**15, 8, 1)
    assert salt and digest


@pytest.mark.parametrize(
    "corrupt",
    [
        "",
        "not-a-hash",
        "scrypt$notanumber$8$1$c2FsdA==$aGFzaA==",
        "bcrypt$1$2$3$c2FsdA==$aGFzaA==",
        "scrypt$32768$8$1$!!!notbase64!!!$aGFzaA==",
    ],
)
def test_corrupt_hash_fails_closed(corrupt: str) -> None:
    """A malformed stored hash is a failed login, not a 500. A crash here
    would take the whole login endpoint down for everyone."""
    assert verify_password("anything", corrupt) is False


# --- Account creation --------------------------------------------------------


@pytest.mark.asyncio
async def test_create_user_rejects_short_passwords(session) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(WeakPasswordError):
        await create_user(
            session,
            username=f"weak-{uuid.uuid4().hex[:8]}",
            password="x" * (MIN_PASSWORD_LENGTH - 1),
            role=UserRole.OPERATOR,
        )


@pytest.mark.asyncio
async def test_duplicate_username_is_rejected(session) -> None:  # type: ignore[no-untyped-def]
    """Uniqueness is enforced by the database index, not a check-then-insert
    that two concurrent creations could both pass."""
    name = f"dupe-{uuid.uuid4().hex[:8]}"
    await create_user(
        session, username=name, password="a-long-enough-password", role=UserRole.OPERATOR
    )
    await session.commit()
    with pytest.raises(UserExistsError):
        await create_user(
            session,
            username=name,
            password="a-long-enough-password",
            role=UserRole.ADMIN,
        )


@pytest.mark.asyncio
async def test_stored_password_is_never_the_plaintext(session) -> None:  # type: ignore[no-untyped-def]
    password = "plaintext-should-not-appear"
    user = await create_user(
        session,
        username=f"hash-{uuid.uuid4().hex[:8]}",
        password=password,
        role=UserRole.OPERATOR,
    )
    await session.commit()
    assert password not in user.password_hash
    assert user.password_hash.startswith("scrypt$")


# --- Login -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_returns_a_working_token(anon_client: AsyncClient) -> None:
    body = await login(anon_client)
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["user"]["username"] == TEST_OPERATOR_USERNAME
    assert body["user"]["role"] == "OPERATOR"
    assert "password" not in str(body).lower() or "password_hash" not in str(body)

    resp = await anon_client.get(
        "/auth/me", headers=auth_headers(body["access_token"])
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["username"] == TEST_OPERATOR_USERNAME


@pytest.mark.asyncio
async def test_login_records_last_login(anon_client: AsyncClient) -> None:
    first = await login(anon_client)
    assert first["user"]["last_login_at"] is not None


@pytest.mark.asyncio
async def test_wrong_password_and_unknown_user_are_indistinguishable(
    anon_client: AsyncClient,
) -> None:
    """Identical status and message for both, so the endpoint is not a
    username oracle. (services.users equalizes the timing separately.)"""
    wrong = await anon_client.post(
        "/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": "definitely-wrong"},
    )
    missing = await anon_client.post(
        "/auth/login",
        json={"username": "no-such-user-at-all", "password": TEST_USER_PASSWORD},
    )
    assert wrong.status_code == 401
    assert missing.status_code == 401
    assert wrong.json()["detail"] == missing.json()["detail"]


@pytest.mark.asyncio
async def test_login_response_never_carries_the_hash(anon_client: AsyncClient) -> None:
    body = await login(anon_client)
    assert "password_hash" not in body["user"]
    assert "scrypt$" not in str(body)


# --- Enrollment token administration ----------------------------------------


@pytest.mark.asyncio
async def test_admin_can_list_tokens_without_seeing_secrets(
    anon_client: AsyncClient,
) -> None:
    """The list view is metadata only: the raw token existed exactly once, in
    the mint response, and the hash is a credential-equivalent lookup key."""
    raw = await mint_enrollment_token(anon_client, created_by="listing-test")

    resp = await anon_client.get(
        "/auth/enrollment-tokens",
        headers=auth_headers(anon_client.admin_token),  # type: ignore[attr-defined]
    )
    assert resp.status_code == 200, resp.text
    tokens = resp.json()["tokens"]
    assert len(tokens) == 1
    entry = tokens[0]
    assert entry["created_by"] == "listing-test"
    assert entry["status"] == "active"
    assert "token" not in entry
    assert "token_hash" not in entry
    assert raw not in resp.text


@pytest.mark.asyncio
async def test_revoked_token_cannot_enroll_a_node(anon_client: AsyncClient) -> None:
    """The whole point of revocation: a withdrawn token stops working.

    403 rather than 401 — the token is genuine and was presented correctly; an
    admin withdrew it. Reporting "invalid" would send an honest operator
    hunting for a typo.
    """
    raw = await mint_enrollment_token(anon_client, created_by="to-be-revoked")
    listing = await anon_client.get(
        "/auth/enrollment-tokens",
        headers=auth_headers(anon_client.admin_token),  # type: ignore[attr-defined]
    )
    token_id = listing.json()["tokens"][0]["id"]

    revoke = await anon_client.post(
        f"/auth/enrollment-tokens/{token_id}/revoke",
        headers=auth_headers(anon_client.admin_token),  # type: ignore[attr-defined]
    )
    assert revoke.status_code == 200, revoke.text
    assert revoke.json()["status"] == "revoked"
    assert revoke.json()["revoked_at"] is not None

    _key, pub = generate_node_keypair()
    resp = await anon_client.post(
        "/nodes/register",
        json={
            "enrollment_token": raw,
            "public_key": pub,
            "hardware": sample_hardware(with_gpu=False),
            "agent_version": "test-0.1",
        },
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_revoking_twice_is_not_an_error(anon_client: AsyncClient) -> None:
    """Idempotent: the caller's intent already holds, and a retry after a
    dropped response should not look like a failure."""
    await mint_enrollment_token(anon_client)
    listing = await anon_client.get(
        "/auth/enrollment-tokens",
        headers=auth_headers(anon_client.admin_token),  # type: ignore[attr-defined]
    )
    token_id = listing.json()["tokens"][0]["id"]
    headers = auth_headers(anon_client.admin_token)  # type: ignore[attr-defined]

    first = await anon_client.post(
        f"/auth/enrollment-tokens/{token_id}/revoke", headers=headers
    )
    second = await anon_client.post(
        f"/auth/enrollment-tokens/{token_id}/revoke", headers=headers
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["revoked_at"] == second.json()["revoked_at"]


@pytest.mark.asyncio
async def test_revoking_an_already_used_token_conflicts(
    anon_client: AsyncClient,
) -> None:
    """Revoking after enrollment would not remove the node's access, so
    reporting success would imply something that did not happen."""
    raw = await mint_enrollment_token(anon_client)
    _key, pub = generate_node_keypair()
    reg = await anon_client.post(
        "/nodes/register",
        json={
            "enrollment_token": raw,
            "public_key": pub,
            "hardware": sample_hardware(with_gpu=False),
            "agent_version": "test-0.1",
        },
    )
    assert reg.status_code == 201

    headers = auth_headers(anon_client.admin_token)  # type: ignore[attr-defined]
    listing = await anon_client.get("/auth/enrollment-tokens", headers=headers)
    entry = listing.json()["tokens"][0]
    assert entry["status"] == "used"

    resp = await anon_client.post(
        f"/auth/enrollment-tokens/{entry['id']}/revoke", headers=headers
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_revoking_an_unknown_token_is_404(anon_client: AsyncClient) -> None:
    resp = await anon_client.post(
        f"/auth/enrollment-tokens/{uuid.uuid4()}/revoke",
        headers={"X-Admin-Key": TEST_ADMIN_KEY},
    )
    assert resp.status_code == 404, resp.text
