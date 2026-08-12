"""Verification of Google ID tokens (ADR-012 addendum).

This module answers exactly one question: *did Google really assert this
identity, recently, to us?* It performs no database work and grants no access —
:mod:`orchestrator.services.users` decides whether the verified identity maps to
an account, and that separation is deliberate: proving who someone is and
deciding what they may do are different failures with different consequences.

What is checked, and why each one matters
-----------------------------------------
=========================  ====================================================
Signature (RS256 via JWKS) Without it the token is a JSON blob anyone can type.
``iss``                    Must be Google. A token from another issuer that
                           happens to be validly signed by *its* key is not
                           Google's assertion.
``aud`` == our client ID   **The critical check.** Google signs ID tokens for
                           every one of its clients with the same keys. A token
                           minted for some unrelated application verifies
                           perfectly against Google's JWKS, so without this an
                           attacker signs in to *their* app and replays the
                           token here.
``exp`` / ``iat``          Expiry, plus our own freshness bound (see below).
``email_verified``         An unverified email is a string the account holder
                           typed, not something Google confirmed they control.
                           Matching an account on it would let anyone claim any
                           address.
``sub`` present            The immutable identifier the account is bound to.
=========================  ====================================================

Freshness beyond ``exp``
------------------------
Google mints ID tokens with roughly an hour of validity and puts nothing in them
that makes one single-use. Within that hour the token is a bearer credential:
whoever holds it can present it here. We additionally require ``iat`` to be
recent (``google_id_token_max_age_seconds``, default 5 minutes), because a real
sign-in posts the token seconds after receiving it. This shrinks the replay
window by an order of magnitude for free.

It does not *eliminate* replay, and this module does not pretend otherwise. Full
protection needs a server-issued, single-use nonce echoed in the token, which
means persisting nonce state on the login path. That is a real gap, recorded in
the addendum rather than papered over — the same treatment ADR-012 gave the
``sessionStorage`` trade-off.

Clock skew
----------
``exp`` and ``iat`` are checked with a small leeway. A peer laptop's clock is
routinely a few seconds off, and rejecting a valid sign-in over that would be a
mysterious, intermittent failure. The leeway is bounded and explicit rather than
disabling the check.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Final

import httpx
import jwt

#: Google's OpenID configuration hardcodes these. They are Google's, not the
#: deployment's, so they are constants rather than settings — making them
#: configurable would only create a way to point verification at an attacker's
#: key set.
_GOOGLE_JWKS_URL: Final = "https://www.googleapis.com/oauth2/v3/certs"
#: Google issues ID tokens under both spellings and treats them as equivalent.
_GOOGLE_ISSUERS: Final = frozenset(
    {"accounts.google.com", "https://accounts.google.com"}
)
_ALGORITHMS: Final = ["RS256"]
#: Tolerance for ``exp``/``iat`` against our clock. Seconds, deliberately small.
_CLOCK_SKEW_LEEWAY_SECONDS: Final = 30


class GoogleTokenError(Exception):
    """A presented ID token is not a valid, fresh Google assertion.

    Carries a reason for the server log. Callers must **not** forward the reason
    to the client: the sign-in endpoint returns one flat message for every
    failure so a prober learns nothing about which check tripped.
    """


class GoogleUnavailableError(Exception):
    """Google's signing keys could not be fetched.

    Distinct from :class:`GoogleTokenError` because the causes are opposite: this
    is our dependency being unreachable, not the caller presenting something
    bad. It maps to 503, not 401 — telling a user "invalid login" when the real
    problem is that the orchestrator has no internet would send them hunting for
    a credential fault that does not exist.
    """


@dataclass(frozen=True, slots=True)
class GoogleIdentity:
    """The verified subset of an ID token's claims that this system uses."""

    #: Google's immutable subject identifier for the account.
    subject: str
    #: Lowercased, Google-verified email address.
    email: str
    #: Display name, when Google supplied one. Never required.
    name: str | None


class _JwksCache:
    """Process-wide cache of Google's signing keys.

    Refetches when the TTL lapses **or** when a token names a ``kid`` the cache
    does not hold — that second trigger is what makes a key rotation
    self-healing instead of an outage lasting until the TTL happens to expire.

    Not shared between processes. Multiple orchestrator replicas each keep their
    own copy, which costs one extra fetch per replica per TTL and is correct
    either way, since the cache holds public keys and no decision depends on two
    replicas agreeing.
    """

    def __init__(self) -> None:
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float = 0.0

    def _is_stale(self, *, ttl_seconds: int, now: float) -> bool:
        return not self._keys or (now - self._fetched_at) >= ttl_seconds

    async def _refresh(self, *, timeout_seconds: float) -> None:
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                response = await client.get(_GOOGLE_JWKS_URL)
                response.raise_for_status()
                document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GoogleUnavailableError(
                f"could not fetch Google signing keys: {exc}"
            ) from exc

        try:
            key_set = jwt.PyJWKSet.from_dict(document)
        except jwt.PyJWKSetError as exc:
            raise GoogleUnavailableError(
                f"Google returned an unusable key set: {exc}"
            ) from exc

        # Keep the previous keys on an empty response rather than emptying the
        # cache: a momentarily odd answer from Google should not turn every
        # subsequent sign-in into a hard failure.
        fresh = {key.key_id: key for key in key_set.keys if key.key_id}
        if not fresh:
            raise GoogleUnavailableError("Google returned no usable signing keys")

        self._keys = fresh
        self._fetched_at = time.monotonic()

    async def get_key(
        self, kid: str, *, ttl_seconds: int, timeout_seconds: float
    ) -> jwt.PyJWK:
        """Return the signing key for ``kid``, refetching if stale or unknown."""
        now = time.monotonic()
        if self._is_stale(ttl_seconds=ttl_seconds, now=now):
            await self._refresh(timeout_seconds=timeout_seconds)

        key = self._keys.get(kid)
        if key is None:
            # Unknown kid on a warm cache: Google has almost certainly rotated.
            # One forced refetch distinguishes a rotation from a bogus token.
            await self._refresh(timeout_seconds=timeout_seconds)
            key = self._keys.get(kid)

        if key is None:
            raise GoogleTokenError(f"token signed by an unknown key id {kid!r}")
        return key

    def clear(self) -> None:
        """Drop the cached keys. For tests, and for an explicit rotation nudge."""
        self._keys = {}
        self._fetched_at = 0.0


#: Process-wide instance. Module-level so the keys survive across requests.
_jwks_cache = _JwksCache()


def reset_jwks_cache() -> None:
    """Clear the process-wide JWKS cache. Used by tests for isolation."""
    _jwks_cache.clear()


def _require_claim(claims: dict[str, Any], name: str) -> Any:
    value = claims.get(name)
    if value is None:
        raise GoogleTokenError(f"token is missing the {name!r} claim")
    return value


async def verify_google_id_token(
    raw_token: str,
    *,
    client_id: str,
    max_age_seconds: int,
    jwks_cache_seconds: int,
    jwks_timeout_seconds: float,
    now: float | None = None,
) -> GoogleIdentity:
    """Verify a Google ID token and return the identity it asserts.

    Raises :class:`GoogleTokenError` if the token is not a valid, fresh Google
    assertion addressed to ``client_id``, or :class:`GoogleUnavailableError` if
    Google's keys could not be fetched at all.
    """
    try:
        header = jwt.get_unverified_header(raw_token)
    except jwt.PyJWTError as exc:
        raise GoogleTokenError(f"unparseable token header: {exc}") from exc

    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise GoogleTokenError("token header carries no key id")

    key = await _jwks_cache.get_key(
        kid, ttl_seconds=jwks_cache_seconds, timeout_seconds=jwks_timeout_seconds
    )

    try:
        claims: dict[str, Any] = jwt.decode(
            raw_token,
            key.key,
            algorithms=_ALGORITHMS,
            # PyJWT enforces the audience match itself. Passing it here rather
            # than comparing afterwards means the check cannot be forgotten and
            # cannot be bypassed by an unexpected claim shape (a list `aud`).
            audience=client_id,
            leeway=_CLOCK_SKEW_LEEWAY_SECONDS,
            options={
                "require": ["exp", "iat", "aud", "iss", "sub"],
                "verify_exp": True,
                "verify_aud": True,
                "verify_signature": True,
            },
        )
    except jwt.PyJWTError as exc:
        # Expired, wrong audience, bad signature, missing required claim.
        raise GoogleTokenError(f"token rejected: {exc}") from exc

    issuer = _require_claim(claims, "iss")
    if issuer not in _GOOGLE_ISSUERS:
        raise GoogleTokenError(f"unexpected issuer {issuer!r}")

    issued_at = _require_claim(claims, "iat")
    if not isinstance(issued_at, int | float):
        raise GoogleTokenError("'iat' is not a number")
    reference = time.time() if now is None else now
    age = reference - float(issued_at)
    if age > max_age_seconds + _CLOCK_SKEW_LEEWAY_SECONDS:
        raise GoogleTokenError(
            f"token was issued {int(age)}s ago, beyond the "
            f"{max_age_seconds}s freshness bound"
        )

    # A token from the future by more than the skew leeway is not something a
    # correct issuer produces; treat it as forged rather than tolerate it.
    if age < -_CLOCK_SKEW_LEEWAY_SECONDS:
        raise GoogleTokenError("token 'iat' is in the future")

    # Google sends email_verified as a real bool, but it has historically been a
    # string in some OIDC providers' tokens. Accept only a genuine True so a
    # truthy "false" can never pass.
    if claims.get("email_verified") is not True:
        raise GoogleTokenError("token does not carry a Google-verified email")

    email = _require_claim(claims, "email")
    if not isinstance(email, str) or "@" not in email:
        raise GoogleTokenError("token 'email' is not an address")

    subject = _require_claim(claims, "sub")
    if not isinstance(subject, str) or not subject:
        raise GoogleTokenError("token 'sub' is empty")

    name = claims.get("name")
    return GoogleIdentity(
        subject=subject,
        email=email.strip().lower(),
        name=name if isinstance(name, str) and name.strip() else None,
    )
