"""Checkpoint-to-MinIO: atomic blob put + versioned manifest (ADR-006, M6).

This module is deliberately **torch-free at import time** so the manifest
atomicity logic — the correctness-critical part — is unit-testable in the dev
venv with a fake object store, without pulling in torch or boto3 (see
``tests/test_checkpoint_manifest.py``). The trainer (``train.py``) does the
``torch.save``/``torch.load`` into/from a bytes buffer and hands this module
only bytes; boto3 is imported lazily, only when a real ``S3ObjectStore`` is
constructed inside the trainer container.

Design (see ``docs/adr/ADR-006-addendum.md`` for the full schema):

* Blob key ``checkpoints/<job_id>/e<epoch>-s<step>-<uuid8>.pt`` is unique per
  write — an interrupted write never overwrites an existing checkpoint.
* Manifest ``manifests/<job_id>.json`` is the single source of truth for "the
  latest good checkpoint" (ADR-006), never a bucket listing.
* :func:`save_checkpoint` PUTs the blob **first**; only on its success does it
  read-modify-write the manifest. A failed blob upload therefore never advances
  the manifest — the invariant the atomicity test pins.
* The manifest read-modify-write is race-free by ADR-006's single-writer
  guarantee (only rank 0 writes, and a reassigned attempt's old rank 0 is dead
  before the new one starts).

Two stores implement the same two-method Protocol. :class:`ApiObjectStore` is
the normal one since ADR-006 addendum 3: it goes through the orchestrator with
a token scoped to this lease, so a peer never holds storage credentials.
:class:`S3ObjectStore` talks to MinIO directly and remains for a co-located
setup that sets ``S3_*`` explicitly. :func:`store_from_env` picks.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

#: Manifest JSON schema version (bumped only on a breaking manifest change).
MANIFEST_SCHEMA_VERSION = 1


class ObjectNotFoundError(Exception):
    """The requested object key does not exist in the store (S3 NoSuchKey)."""


class ObjectStore(Protocol):
    """The minimal object-store surface checkpointing needs. Real code uses
    :class:`S3ObjectStore`; tests inject a fake implementing this Protocol."""

    def put_bytes(self, key: str, data: bytes) -> None: ...

    def get_bytes(self, key: str) -> bytes:
        """Return the object's bytes, or raise :class:`ObjectNotFoundError`."""
        ...


def manifest_key(job_id: str) -> str:
    return f"manifests/{job_id}.json"


def blob_key(job_id: str, *, epoch: int, step: int) -> str:
    """A unique, versioned checkpoint blob key. The uuid8 suffix guarantees a
    write never collides with or overwrites a prior checkpoint."""
    return f"checkpoints/{job_id}/e{epoch:03d}-s{step:08d}-{uuid.uuid4().hex[:8]}.pt"


@dataclass(frozen=True)
class CheckpointEntry:
    """One manifest entry — enough metadata to identify a resume target
    (ADR-006: step, epoch, cohort/world_size, timestamp)."""

    key: str
    step: int
    epoch: int  # the epoch index to *resume into* (loop start_epoch)
    world_size: int
    lease_epoch: int | None
    timestamp_utc: str
    loss: float | None


def _entry_from_dict(d: dict[str, Any]) -> CheckpointEntry:
    """Build an entry from a manifest dict, tolerant of missing optional keys."""
    return CheckpointEntry(
        key=str(d["key"]),
        step=int(d["step"]),
        epoch=int(d["epoch"]),
        world_size=int(d.get("world_size", 1)),
        lease_epoch=d.get("lease_epoch"),
        timestamp_utc=str(d.get("timestamp_utc", "")),
        loss=d.get("loss"),
    )


def read_manifest(store: ObjectStore, job_id: str) -> dict[str, Any] | None:
    """Return the parsed manifest for ``job_id``, or ``None`` if none exists."""
    try:
        raw = store.get_bytes(manifest_key(job_id))
    except ObjectNotFoundError:
        return None
    parsed: dict[str, Any] = json.loads(raw.decode("utf-8"))
    return parsed


def latest_entry(store: ObjectStore, job_id: str) -> CheckpointEntry | None:
    """The latest good checkpoint entry for ``job_id`` from its manifest, or
    ``None`` when there is no checkpoint to resume from (first attempt)."""
    manifest = read_manifest(store, job_id)
    if not manifest:
        return None
    latest = manifest.get("latest")
    if not latest:
        return None
    return _entry_from_dict(latest)


def save_checkpoint(
    store: ObjectStore,
    *,
    job_id: str,
    blob: bytes,
    step: int,
    epoch: int,
    world_size: int,
    lease_epoch: int | None,
    loss: float | None,
) -> CheckpointEntry:
    """Atomically persist one checkpoint: PUT the blob, then (only on success)
    append it to the manifest and repoint ``latest``. Returns the new entry.

    If the blob PUT raises, the manifest is untouched — it keeps pointing at the
    previous good checkpoint, never a partial one (the atomicity invariant).
    """
    key = blob_key(job_id, epoch=epoch, step=step)

    # 1. Atomic blob put FIRST. If this raises, we fall through without ever
    #    touching the manifest.
    store.put_bytes(key, blob)

    # 2. Only now read-modify-write the manifest.
    entry = CheckpointEntry(
        key=key,
        step=step,
        epoch=epoch,
        world_size=world_size,
        lease_epoch=lease_epoch,
        timestamp_utc=datetime.now(UTC).isoformat(),
        loss=loss,
    )
    entry_dict = asdict(entry)
    manifest = read_manifest(store, job_id) or {
        "schema": MANIFEST_SCHEMA_VERSION,
        "job_id": job_id,
        "checkpoints": [],
    }
    checkpoints = manifest.setdefault("checkpoints", [])
    checkpoints.append(entry_dict)
    manifest["latest"] = entry_dict
    manifest["updated_utc"] = datetime.now(UTC).isoformat()
    store.put_bytes(
        manifest_key(job_id), json.dumps(manifest).encode("utf-8")
    )
    return entry


class S3ObjectStore:
    """boto3-backed :class:`ObjectStore` for MinIO / any S3-compatible backend.

    boto3/botocore are imported lazily in ``__init__`` so importing this module
    (for the pure manifest logic / tests) never requires them.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "us-east-1",
    ) -> None:
        import boto3
        from botocore.client import Config

        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(signature_version="s3v4"),
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        import contextlib

        import botocore.exceptions

        try:
            self._client.head_bucket(Bucket=self._bucket)
        except botocore.exceptions.ClientError:
            # A concurrent create, or an already-owned bucket, is fine either way.
            with contextlib.suppress(botocore.exceptions.ClientError):
                self._client.create_bucket(Bucket=self._bucket)

    def put_bytes(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data)

    def get_bytes(self, key: str) -> bytes:
        import botocore.exceptions

        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
        except botocore.exceptions.ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in ("NoSuchKey", "NoSuchBucket", "404"):
                raise ObjectNotFoundError(key) from exc
            raise
        body: bytes = resp["Body"].read()
        return body

    @classmethod
    def from_env(cls, env: dict[str, str]) -> S3ObjectStore | None:
        """Build a store from the trainer container's env, or ``None`` when S3
        checkpointing is not configured (``S3_ENDPOINT_URL`` absent) — in which
        case the trainer runs without checkpoint/resume, exactly like M4/M5."""
        endpoint = env.get("S3_ENDPOINT_URL")
        access_key = env.get("S3_ACCESS_KEY")
        secret_key = env.get("S3_SECRET_KEY")
        bucket = env.get("S3_BUCKET_CHECKPOINTS")
        if not (endpoint and access_key and secret_key and bucket):
            return None
        return cls(
            endpoint_url=endpoint,
            access_key=access_key,
            secret_key=secret_key,
            bucket=bucket,
            region=env.get("S3_REGION", "us-east-1"),
        )


class ApiObjectStore:
    """:class:`ObjectStore` over the orchestrator's checkpoint endpoints.

    ``PUT``/``GET /leases/{lease_id}/checkpoint-objects/{key}``, authorised by
    the lease-scoped token the agent received at claim time (ADR-006 addendum
    3). Uses only the standard library, so the trainer gains no dependency.

    The orchestrator answers 409 once this attempt has been superseded. That
    surfaces as an exception from ``put_bytes``, which ``train.py`` already
    treats as a failed write -- logged, and training continues until the agent
    stops the container.
    """

    def __init__(
        self, *, api_url: str, lease_id: str, token: str, timeout_seconds: float = 120.0
    ) -> None:
        self._base = f"{api_url.rstrip('/')}/leases/{lease_id}/checkpoint-objects/"
        self._token = token
        self._timeout = timeout_seconds

    def _request(self, method: str, key: str, data: bytes | None = None) -> bytes:
        request = urllib.request.Request(
            self._base + urllib.parse.quote(key, safe="/"),
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/octet-stream",
            },
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            body: bytes = response.read()
            return body

    def put_bytes(self, key: str, data: bytes) -> None:
        self._request("PUT", key, data)

    def get_bytes(self, key: str) -> bytes:
        try:
            return self._request("GET", key)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ObjectNotFoundError(key) from exc
            raise


def store_from_env(env: dict[str, str]) -> ObjectStore | None:
    """The checkpoint store this trainer should use, or ``None`` for none.

    The orchestrator route wins when the agent supplied it; direct S3 is the
    fallback for a setup that configures MinIO credentials explicitly.
    """
    api_url = env.get("CHECKPOINT_API_URL")
    token = env.get("CHECKPOINT_TOKEN")
    lease_id = env.get("LEASE_ID")
    if api_url and token and lease_id:
        return ApiObjectStore(api_url=api_url, lease_id=lease_id, token=token)
    return S3ObjectStore.from_env(env)
