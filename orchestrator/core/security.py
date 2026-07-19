"""Cryptographic primitives for identity and auth (ADR-008).

Pure, side-effect-free helpers: opaque-token minting and hashing, HS256 JWT
issue/verify, and Ed25519 public-key loading and signature verification. No
database or FastAPI concerns live here.
"""

from __future__ import annotations

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
