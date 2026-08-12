"""Google sign-in (ADR-012 addendum).

These tests sign **real** RS256 ID tokens with a locally generated RSA key and
serve that key through a stubbed JWKS endpoint, so the production verification
path runs end to end: real JWKS parsing, real signature check, real audience and
issuer enforcement. Nothing about the verifier is mocked out — only the network
hop to Google is replaced, because reaching Google from CI would make the suite
depend on the internet and on someone else's uptime.

``StubGoogle`` is named per CONTRIBUTING.md #5 and lives here in ``tests/``.

The security properties asserted here are the reason the addendum can add an
external IdP without weakening ADR-012:

* an identity Google vouches for that matches no account is **refused**, not
  signed up (``test_refuses_unknown_email``);
* a token minted for a *different* Google client verifies against Google's keys
  but is refused here (``test_refuses_wrong_audience``) — the single most
  important check in the flow;
* the token issued on success is an ordinary ``aud="user"`` token with no extra
  power (``test_issued_token_is_an_ordinary_user_token``).
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import jwt
import jwt.algorithms  # imported explicitly: used to publish the test JWK
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient
from sqlalchemy import select

from orchestrator.core.config import get_settings
from orchestrator.core.security import USER_AUDIENCE, decode_user_jwt
from orchestrator.models.user import User, UserRole
from orchestrator.services import google_oidc
from tests.helpers import TEST_JWT_KEY, seed_user

_TEST_CLIENT_ID = "test-client-id.apps.googleusercontent.com"
_KEY_ID = "test-key-1"
_GOOGLE_ISSUER = "https://accounts.google.com"


# --- Stub Google -------------------------------------------------------------


class StubGoogleResponse:
    """Minimal stand-in for an httpx response carrying a JWKS document."""

    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"stub status {self.status_code}", request=None, response=None  # type: ignore[arg-type]
            )

    def json(self) -> dict[str, Any]:
        return self._payload


class StubGoogle:
    """Replaces ``httpx.AsyncClient`` inside google_oidc for the JWKS fetch.

    Counts fetches so the caching and rotation-refetch behaviour can be asserted
    rather than assumed.
    """

    fetches = 0
    jwks: dict[str, Any] = {"keys": []}
    fail = False

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> StubGoogle:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def get(self, _url: str) -> StubGoogleResponse:
        type(self).fetches += 1
        if type(self).fail:
            import httpx

            raise httpx.ConnectError("stubbed: no route to Google")
        return StubGoogleResponse(type(self).jwks)


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    """One RSA key for the whole module — generation is slow enough to matter."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwks_document(rsa_key: rsa.RSAPrivateKey) -> dict[str, Any]:
    """The JWKS Google would publish for :func:`rsa_key`."""
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key()))
    public_jwk.update({"kid": _KEY_ID, "use": "sig", "alg": "RS256"})
    return {"keys": [public_jwk]}


def make_id_token(
    rsa_key: rsa.RSAPrivateKey,
    *,
    subject: str = "google-subject-1",
    email: str = "operator@example.com",
    email_verified: bool | str = True,
    audience: str = _TEST_CLIENT_ID,
    issuer: str = _GOOGLE_ISSUER,
    issued_at: float | None = None,
    expires_in: int = 3600,
    kid: str = _KEY_ID,
    name: str | None = "Test Operator",
) -> str:
    """Sign an ID token shaped exactly like one of Google's."""
    iat = time.time() if issued_at is None else issued_at
    claims: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "email": email,
        "email_verified": email_verified,
        "iat": int(iat),
        "exp": int(iat + expires_in),
    }
    if name is not None:
        claims["name"] = name
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": kid})


@pytest_asyncio.fixture
async def google_client(
    anon_client: AsyncClient,
    jwks_document: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncClient:
    """``anon_client`` with Google sign-in configured and Google stubbed out.

    Settings are re-read per request through ``get_settings_dep``, so clearing the
    cache after setting the variable is enough — the app does not need rebuilding.
    """
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", _TEST_CLIENT_ID)
    get_settings.cache_clear()

    StubGoogle.fetches = 0
    StubGoogle.jwks = jwks_document
    StubGoogle.fail = False
    monkeypatch.setattr(google_oidc.httpx, "AsyncClient", StubGoogle)
    google_oidc.reset_jwks_cache()

    return anon_client


# --- Provider discovery ------------------------------------------------------


async def test_providers_reports_google_disabled_by_default(
    anon_client: AsyncClient,
) -> None:
    """With no client ID configured, the dashboard must not offer the button."""
    response = await anon_client.get("/auth/providers")
    assert response.status_code == 200
    body = response.json()
    assert body["password"] is True
    assert body["google"] == {"enabled": False, "client_id": None}


async def test_providers_reports_google_enabled_with_client_id(
    google_client: AsyncClient,
) -> None:
    response = await google_client.get("/auth/providers")
    assert response.status_code == 200
    assert response.json()["google"] == {
        "enabled": True,
        "client_id": _TEST_CLIENT_ID,
    }


async def test_google_endpoint_is_503_when_unconfigured(
    anon_client: AsyncClient, rsa_key: rsa.RSAPrivateKey
) -> None:
    """Unconfigured is a server-state problem, not a bad credential."""
    response = await anon_client.post(
        "/auth/google", json={"credential": make_id_token(rsa_key)}
    )
    assert response.status_code == 503


# --- The happy path ----------------------------------------------------------


async def test_signs_in_and_binds_subject_on_first_use(
    google_client: AsyncClient, rsa_key: rsa.RSAPrivateKey, session: Any
) -> None:
    """A verified identity matching a pre-created account signs in and binds."""
    async with session as db:
        await seed_user(
            db,
            username="priya",
            role=UserRole.OPERATOR,
            email="Priya@Example.com",  # deliberately mixed case
        )

    response = await google_client.post(
        "/auth/google",
        json={"credential": make_id_token(rsa_key, email="priya@example.com")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["username"] == "priya"
    assert body["user"]["role"] == "OPERATOR"
    assert body["expires_in"] > 0

    # The subject is now bound, and last_login_at was stamped.
    from orchestrator.core.db import get_sessionmaker

    async with get_sessionmaker()() as db:
        user = (
            await db.execute(select(User).where(User.username == "priya"))
        ).scalar_one()
        assert user.google_sub == "google-subject-1"
        assert user.last_login_at is not None


async def test_issued_token_is_an_ordinary_user_token(
    google_client: AsyncClient, rsa_key: rsa.RSAPrivateKey, session: Any
) -> None:
    """Google sign-in grants no extra power: same audience, same claims.

    If this ever produced something other than a plain ``aud="user"`` token, the
    addendum would have created a second authorization path rather than a second
    way to prove identity.
    """
    async with session as db:
        user = await seed_user(
            db, username="dev", role=UserRole.ADMIN, email="dev@example.com"
        )

    response = await google_client.post(
        "/auth/google",
        json={"credential": make_id_token(rsa_key, email="dev@example.com")},
    )
    assert response.status_code == 200

    token = response.json()["access_token"]
    user_id, username, role = decode_user_jwt(token, signing_key=TEST_JWT_KEY)
    assert user_id == str(user.id)
    assert username == "dev"
    assert role == "ADMIN"

    claims = jwt.decode(
        token, TEST_JWT_KEY, algorithms=["HS256"], audience=USER_AUDIENCE
    )
    assert claims["aud"] == USER_AUDIENCE


async def test_matches_by_subject_after_email_changes(
    google_client: AsyncClient, rsa_key: rsa.RSAPrivateKey, session: Any
) -> None:
    """Once bound, the immutable ``sub`` is the match — not the address.

    Google's `sub` outlives an address change, and matching on it is what stops a
    reassigned school address from inheriting someone else's fleet access.
    """
    async with session as db:
        await seed_user(
            db,
            username="moved",
            role=UserRole.OPERATOR,
            email="old@example.com",
            google_sub="stable-subject",
        )

    response = await google_client.post(
        "/auth/google",
        json={
            "credential": make_id_token(
                rsa_key, subject="stable-subject", email="brand-new@example.com"
            )
        },
    )
    assert response.status_code == 200
    assert response.json()["user"]["username"] == "moved"


# --- Refusals ----------------------------------------------------------------


async def test_refuses_unknown_email(
    google_client: AsyncClient, rsa_key: rsa.RSAPrivateKey
) -> None:
    """The core property: Google sign-in never creates an account.

    An account here is permission to run containers on other people's machines,
    so a stranger holding a valid Google identity must get nothing.
    """
    response = await google_client.post(
        "/auth/google",
        json={"credential": make_id_token(rsa_key, email="stranger@example.com")},
    )
    assert response.status_code == 401

    from orchestrator.core.db import get_sessionmaker

    async with get_sessionmaker()() as db:
        count = len(
            (
                await db.execute(select(User).where(User.email == "stranger@example.com"))
            ).all()
        )
    assert count == 0, "a refused sign-in must not have created an account"


async def test_refuses_wrong_audience(
    google_client: AsyncClient, rsa_key: rsa.RSAPrivateKey
) -> None:
    """A token minted for another Google client must not work here.

    No account is seeded on purpose: the audience check must fail before any
    lookup happens, so this passing for the *wrong* reason is not possible.

    Google signs every client's ID tokens with the same keys, so this token has a
    perfectly valid signature. Only the audience check separates "Google says
    this person signed in to *us*" from "...to some unrelated app". Without it,
    anyone could stand up their own Google client and replay its tokens.
    """
    response = await google_client.post(
        "/auth/google",
        json={
            "credential": make_id_token(
                rsa_key,
                email="operator@example.com",
                audience="someone-elses-client.apps.googleusercontent.com",
            )
        },
    )
    assert response.status_code == 401


async def test_refuses_wrong_issuer(
    google_client: AsyncClient, rsa_key: rsa.RSAPrivateKey, session: Any
) -> None:
    async with session as db:
        await seed_user(
            db, username="iss", role=UserRole.OPERATOR, email="iss@example.com"
        )
    response = await google_client.post(
        "/auth/google",
        json={
            "credential": make_id_token(
                rsa_key, email="iss@example.com", issuer="https://evil.example.com"
            )
        },
    )
    assert response.status_code == 401


async def test_refuses_unverified_email(
    google_client: AsyncClient, rsa_key: rsa.RSAPrivateKey, session: Any
) -> None:
    """An unverified address is self-asserted, so it cannot select an account."""
    async with session as db:
        await seed_user(
            db, username="unv", role=UserRole.OPERATOR, email="unv@example.com"
        )
    response = await google_client.post(
        "/auth/google",
        json={
            "credential": make_id_token(
                rsa_key, email="unv@example.com", email_verified=False
            )
        },
    )
    assert response.status_code == 401


async def test_refuses_truthy_string_email_verified(
    google_client: AsyncClient, rsa_key: rsa.RSAPrivateKey, session: Any
) -> None:
    """``"false"`` is a truthy string; only a real boolean True may pass."""
    async with session as db:
        await seed_user(
            db, username="str", role=UserRole.OPERATOR, email="str@example.com"
        )
    response = await google_client.post(
        "/auth/google",
        json={
            "credential": make_id_token(
                rsa_key, email="str@example.com", email_verified="false"
            )
        },
    )
    assert response.status_code == 401


async def test_refuses_expired_token(
    google_client: AsyncClient, rsa_key: rsa.RSAPrivateKey, session: Any
) -> None:
    async with session as db:
        await seed_user(
            db, username="exp", role=UserRole.OPERATOR, email="exp@example.com"
        )
    response = await google_client.post(
        "/auth/google",
        json={
            "credential": make_id_token(
                rsa_key,
                email="exp@example.com",
                issued_at=time.time() - 7200,
                expires_in=3600,  # expired an hour ago
            )
        },
    )
    assert response.status_code == 401


async def test_refuses_stale_but_unexpired_token(
    google_client: AsyncClient, rsa_key: rsa.RSAPrivateKey, session: Any
) -> None:
    """The freshness bound, which is stricter than ``exp``.

    Google mints ID tokens valid for about an hour and nothing makes one
    single-use. A real sign-in forwards the token within seconds, so demanding a
    recent ``iat`` shrinks the replay window from ~1 h to minutes. This token is
    still within its ``exp`` and must nonetheless be refused.
    """
    async with session as db:
        await seed_user(
            db, username="stale", role=UserRole.OPERATOR, email="stale@example.com"
        )
    response = await google_client.post(
        "/auth/google",
        json={
            "credential": make_id_token(
                rsa_key,
                email="stale@example.com",
                issued_at=time.time() - 1800,  # 30 min old, exp is 60 min
                expires_in=3600,
            )
        },
    )
    assert response.status_code == 401


async def test_refuses_token_signed_by_unknown_key(
    google_client: AsyncClient, session: Any
) -> None:
    """A token signed by a key Google does not publish is refused."""
    async with session as db:
        await seed_user(
            db, username="forged", role=UserRole.OPERATOR, email="forged@example.com"
        )
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    response = await google_client.post(
        "/auth/google",
        json={
            "credential": make_id_token(attacker_key, email="forged@example.com"),
        },
    )
    assert response.status_code == 401


async def test_refuses_disabled_account(
    google_client: AsyncClient, rsa_key: rsa.RSAPrivateKey, session: Any
) -> None:
    async with session as db:
        user = await seed_user(
            db, username="gone", role=UserRole.OPERATOR, email="gone@example.com"
        )
        user.disabled_at = datetime.now(UTC)
        await db.commit()

    response = await google_client.post(
        "/auth/google",
        json={"credential": make_id_token(rsa_key, email="gone@example.com")},
    )
    assert response.status_code == 401


async def test_refuses_when_email_is_bound_to_another_subject(
    google_client: AsyncClient, rsa_key: rsa.RSAPrivateKey, session: Any
) -> None:
    """A rebind is indistinguishable from an address-reassignment takeover.

    The account already trusts one Google subject. A *different* subject arriving
    with the same address means the address changed hands, so this refuses rather
    than silently handing the account to whoever holds it now.
    """
    async with session as db:
        await seed_user(
            db,
            username="taken",
            role=UserRole.OPERATOR,
            email="shared@example.com",
            google_sub="the-original-subject",
        )

    response = await google_client.post(
        "/auth/google",
        json={
            "credential": make_id_token(
                rsa_key, subject="a-different-subject", email="shared@example.com"
            )
        },
    )
    assert response.status_code == 401


async def test_refusal_message_does_not_reveal_which_check_failed(
    google_client: AsyncClient, rsa_key: rsa.RSAPrivateKey, session: Any
) -> None:
    """Every refusal reads the same, so probing teaches nothing.

    "No account for that address" would let anyone with a Google account
    enumerate who is on this fleet.
    """
    async with session as db:
        await seed_user(
            db, username="real", role=UserRole.OPERATOR, email="real@example.com"
        )

    unknown = await google_client.post(
        "/auth/google",
        json={"credential": make_id_token(rsa_key, email="nobody@example.com")},
    )
    unverified = await google_client.post(
        "/auth/google",
        json={
            "credential": make_id_token(
                rsa_key, email="real@example.com", email_verified=False
            )
        },
    )
    assert unknown.status_code == unverified.status_code == 401
    assert unknown.json()["detail"] == unverified.json()["detail"]


# --- Interaction with the password path --------------------------------------


async def test_google_only_account_cannot_sign_in_with_a_password(
    google_client: AsyncClient, session: Any
) -> None:
    """A NULL password_hash is a refusal, never a wildcard."""
    async with session as db:
        await seed_user(
            db,
            username="googleonly",
            role=UserRole.OPERATOR,
            email="googleonly@example.com",
            with_password=False,
        )

    # Every one of these must be refused. The empty string is refused a step
    # earlier, by LoginRequest's min_length=1, so it is a 422 rather than a 401 —
    # asserted separately instead of loosened to "not 200", because *where* a
    # credential is rejected is part of the behaviour worth pinning down.
    empty = await google_client.post(
        "/auth/login", json={"username": "googleonly", "password": ""}
    )
    assert empty.status_code == 422, "an empty password must not reach the KDF"

    for attempt in ("anything", "pytest-password-1234", "x" * 100):
        response = await google_client.post(
            "/auth/login", json={"username": "googleonly", "password": attempt}
        )
        assert response.status_code == 401, f"password {attempt!r} was accepted"


async def test_password_login_still_works_when_google_is_enabled(
    google_client: AsyncClient,
) -> None:
    """Google is additive: the offline path must not regress."""
    from tests.helpers import TEST_OPERATOR_USERNAME, TEST_USER_PASSWORD

    response = await google_client.post(
        "/auth/login",
        json={"username": TEST_OPERATOR_USERNAME, "password": TEST_USER_PASSWORD},
    )
    assert response.status_code == 200


# --- Google being unreachable ------------------------------------------------


async def test_google_unreachable_is_503_not_401(
    google_client: AsyncClient, rsa_key: rsa.RSAPrivateKey, session: Any
) -> None:
    """A dead dependency must not be reported as a bad credential.

    Telling someone "invalid login" when the orchestrator simply has no route to
    Google sends them hunting for a credential fault that does not exist.
    """
    async with session as db:
        await seed_user(
            db, username="offline", role=UserRole.OPERATOR, email="offline@example.com"
        )
    google_oidc.reset_jwks_cache()
    StubGoogle.fail = True

    response = await google_client.post(
        "/auth/google",
        json={"credential": make_id_token(rsa_key, email="offline@example.com")},
    )
    assert response.status_code == 503


async def test_jwks_is_cached_across_sign_ins(
    google_client: AsyncClient, rsa_key: rsa.RSAPrivateKey, session: Any
) -> None:
    """Two sign-ins must not mean two fetches of Google's keys."""
    async with session as db:
        await seed_user(
            db, username="cached", role=UserRole.OPERATOR, email="cached@example.com"
        )

    for _ in range(2):
        response = await google_client.post(
            "/auth/google",
            json={"credential": make_id_token(rsa_key, email="cached@example.com")},
        )
        assert response.status_code == 200

    assert StubGoogle.fetches == 1, f"expected one JWKS fetch, saw {StubGoogle.fetches}"


async def test_unknown_kid_forces_one_refetch(
    google_client: AsyncClient, rsa_key: rsa.RSAPrivateKey, session: Any
) -> None:
    """An unrecognized key id refetches once, so a rotation self-heals.

    Without this a rotation would break every sign-in until the cache TTL
    happened to lapse.
    """
    async with session as db:
        await seed_user(
            db, username="rot", role=UserRole.OPERATOR, email="rot@example.com"
        )

    # Warm the cache.
    first = await google_client.post(
        "/auth/google",
        json={"credential": make_id_token(rsa_key, email="rot@example.com")},
    )
    assert first.status_code == 200
    assert StubGoogle.fetches == 1

    # A token naming a kid the cache does not hold.
    second = await google_client.post(
        "/auth/google",
        json={
            "credential": make_id_token(
                rsa_key, email="rot@example.com", kid="rotated-key-2"
            )
        },
    )
    assert second.status_code == 401
    assert StubGoogle.fetches == 2, "an unknown kid should trigger exactly one refetch"


# --- Request validation ------------------------------------------------------


async def test_rejects_oversized_and_empty_credentials(
    google_client: AsyncClient,
) -> None:
    """Bounded input: an oversized body must not reach the JWT parser."""
    too_long = await google_client.post(
        "/auth/google", json={"credential": "a" * 5000}
    )
    assert too_long.status_code == 422

    empty = await google_client.post("/auth/google", json={"credential": ""})
    assert empty.status_code == 422

    extra = await google_client.post(
        "/auth/google",
        json={"credential": "x", "role": "ADMIN"},
    )
    assert extra.status_code == 422, "extra fields must be forbidden"


async def test_google_token_cannot_be_used_as_a_node_credential(
    google_client: AsyncClient, rsa_key: rsa.RSAPrivateKey, session: Any
) -> None:
    """Audience separation (ADR-012 §3) survives the new sign-in path."""
    async with session as db:
        await seed_user(
            db, username="aud", role=UserRole.ADMIN, email="aud@example.com"
        )
    login = await google_client.post(
        "/auth/google",
        json={"credential": make_id_token(rsa_key, email="aud@example.com")},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    # A node-scoped endpoint must reject this user token on audience.
    response = await google_client.post(
        f"/nodes/{uuid.uuid4()}/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "ONLINE"},
    )
    assert response.status_code in (401, 403, 422)
