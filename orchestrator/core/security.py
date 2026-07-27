"""Cryptographic primitives for identity and auth (ADR-008, ADR-012).

Pure, side-effect-free helpers: opaque-token minting and hashing, password
hashing, HS256 JWT issue/verify, and Ed25519 public-key loading and signature
verification. No database or FastAPI concerns live here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

# JWT audience for node-scoped access tokens. A token minted for any other
# audience must be rejected by require_node_auth.
NODE_AUDIENCE = "node"
# JWT audience for human (dashboard/CLI operator) access tokens. Kept strictly
# distinct from NODE_AUDIENCE: both are signed with the same key, so `aud` is
# the only thing preventing a node's own token from acting as an operator
# (ADR-012 §3). Verified in both directions, never merely present.
USER_AUDIENCE = "user"

_JWT_ALGORITHM = "HS256"


# --- Opaque tokens / nonces --------------------------------------------------


def generate_opaque_token() -> str:
    """Return a fresh, high-entropy URL-safe token (enrollment token / nonce)."""
    return secrets.token_urlsafe(32)


def hash_token(raw_token: str) -> str:
    """Return the SHA-256 hex digest used to store/look up an opaque token.

    Enrollment tokens are stored only as this digest; the raw value is never
    persisted. SHA-256 is appropriate here because the input is full-entropy
    random (not a low-entropy password needing a slow KDF).
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    """Timing-safe string comparison (for the admin API key check)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# --- Passwords (ADR-012) ------------------------------------------------------

#: scrypt cost parameters. n=2**15, r=8, p=1 costs ~32 MB and ~50-100 ms per
#: hash — deliberately slow, because unlike an enrollment token a password is
#: low-entropy and guessable. Stored *inside* each hash string so these can be
#: raised later without invalidating rows written under the old cost.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_SALT_BYTES = 16
_SCRYPT_DKLEN = 32
_SCRYPT_PREFIX = "scrypt"
#: scrypt's memory bound must exceed roughly 128 * n * r bytes or it raises.
_SCRYPT_MAXMEM = 128 * _SCRYPT_N * _SCRYPT_R * 2


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    """Hash a password with scrypt, returning a self-describing hash string.

    Format: ``scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>``. The cost parameters
    travel with the hash so :func:`verify_password` can validate a hash written
    under older parameters (ADR-012 §5).
    """
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return "$".join(
        [_SCRYPT_PREFIX, str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P), _b64(salt), _b64(derived)]
    )


def verify_password(password: str, stored_hash: str) -> bool:
    """Return True iff ``password`` reproduces ``stored_hash``.

    Re-derives using the cost parameters recorded in the stored hash and
    compares in constant time. A malformed or unknown-scheme hash returns
    False rather than raising: a corrupt row must fail closed as a failed
    login, not crash the login endpoint.
    """
    try:
        scheme, n_s, r_s, p_s, salt_b64, hash_b64 = stored_hash.split("$")
        if scheme != _SCRYPT_PREFIX:
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
    except (ValueError, TypeError):
        return False
    try:
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=128 * n * r * 2,
        )
    except ValueError:  # nonsensical cost parameters in a corrupt row
        return False
    return hmac.compare_digest(derived, expected)


# --- JWT (HS256) -------------------------------------------------------------


def create_node_jwt(
    *, node_id: str, signing_key: str, ttl_seconds: int, now: datetime | None = None
) -> str:
    """Issue a short-lived HS256 access token for a node.

    Claims: ``sub`` = node id, ``aud`` = ``"node"``, ``iat``/``exp`` bounding a
    ``ttl_seconds`` window.
    """
    issued = now or datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": node_id,
        "aud": NODE_AUDIENCE,
        "iat": int(issued.timestamp()),
        "exp": int((issued + timedelta(seconds=ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, signing_key, algorithm=_JWT_ALGORITHM)


class JWTValidationError(Exception):
    """Raised when a presented JWT is expired, malformed, or otherwise invalid."""


def decode_node_jwt(token: str, *, signing_key: str) -> str:
    """Verify a node access token and return its subject (node id).

    Enforces signature, expiry, and audience (``"node"``). Raises
    :class:`JWTValidationError` on any failure — callers map that to HTTP 401.
    """
    try:
        payload = jwt.decode(
            token,
            signing_key,
            algorithms=[_JWT_ALGORITHM],
            audience=NODE_AUDIENCE,
            options={"require": ["exp", "sub", "aud"]},
        )
    except jwt.PyJWTError as exc:  # expired, bad signature, wrong audience, ...
        raise JWTValidationError(str(exc)) from exc
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise JWTValidationError("token missing subject")
    return subject


def create_user_jwt(
    *,
    user_id: str,
    username: str,
    role: str,
    signing_key: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> str:
    """Issue a short-lived HS256 access token for a human operator (ADR-012).

    Claims: ``sub`` = user id, ``aud`` = ``"user"``, plus ``username`` and
    ``role`` so authorization decisions need no database round trip on the hot
    read path. The role is re-checked against the live row on privileged
    writes, so a role downgrade takes effect within one token TTL.
    """
    issued = now or datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": user_id,
        "aud": USER_AUDIENCE,
        "username": username,
        "role": role,
        "iat": int(issued.timestamp()),
        "exp": int((issued + timedelta(seconds=ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, signing_key, algorithm=_JWT_ALGORITHM)


def decode_user_jwt(token: str, *, signing_key: str) -> tuple[str, str, str]:
    """Verify a user access token; return ``(user_id, username, role)``.

    Enforces signature, expiry, and audience (``"user"``). A node token is
    rejected here even though it is validly signed, because its audience is
    ``"node"`` — that check is the whole boundary between a peer and an
    operator (ADR-012 §3).
    """
    try:
        payload = jwt.decode(
            token,
            signing_key,
            algorithms=[_JWT_ALGORITHM],
            audience=USER_AUDIENCE,
            options={"require": ["exp", "sub", "aud"]},
        )
    except jwt.PyJWTError as exc:  # expired, bad signature, wrong audience, ...
        raise JWTValidationError(str(exc)) from exc
    subject = payload.get("sub")
    username = payload.get("username")
    role = payload.get("role")
    if not isinstance(subject, str) or not subject:
        raise JWTValidationError("token missing subject")
    if not isinstance(username, str) or not username:
        raise JWTValidationError("token missing username")
    if not isinstance(role, str) or not role:
        raise JWTValidationError("token missing role")
    return subject, username, role


# --- Ed25519 ------------------------------------------------------------------


class PublicKeyError(Exception):
    """Raised when a PEM public key cannot be parsed as an Ed25519 key."""


def load_ed25519_public_key(pem: str) -> Ed25519PublicKey:
    """Parse a PEM SubjectPublicKeyInfo blob as an Ed25519 public key.

    Raises :class:`PublicKeyError` if the PEM is malformed or is some other key
    type — we accept Ed25519 only.
    """
    try:
        key = load_pem_public_key(pem.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise PublicKeyError(f"unparseable public key: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise PublicKeyError("public key is not an Ed25519 key")
    return key


def verify_ed25519_signature(*, pem: str, message: bytes, signature: bytes) -> bool:
    """Return True iff ``signature`` is a valid Ed25519 signature of ``message``.

    Any parse or verification failure returns False — this never raises for a
    bad signature, so callers can branch on the boolean.
    """
    try:
        key = load_ed25519_public_key(pem)
    except PublicKeyError:
        return False
    try:
        key.verify(signature, message)
    except InvalidSignature:
        return False
    return True
