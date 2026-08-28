"""Reading a job's saved model out of object storage (ADR-006 addendum 2).

The orchestrator never writes checkpoints — the trainer does, on a peer — so
this module is read-only. Its whole job is answering "where is this job's model,
and is it safe to hand over?".

The manifest is the authority, not the database
-----------------------------------------------
ADR-006 has the trainer write the blob first and the manifest second, so a
manifest entry exists only once its blob is fully stored. Reading the manifest
therefore cannot point at a half-written file, which is exactly why this reads
storage rather than the ``jobs`` table.

The database is not an alternative here. ``training_log_lines`` and the resume
event record a ``checkpoint_key`` only in the narrower case where a job *resumed*
from one; a job that trained straight through and saved ten checkpoints has none
of them in Postgres. Using the database would silently offer downloads for
resumed jobs and nothing for everyone else.

The manifest is parsed with ``trainer.checkpoint``'s own reader rather than a
second implementation, so the two cannot drift about what a manifest means. That
module is deliberately torch-free at import time, so importing it here does not
drag PyTorch into the control plane.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from orchestrator.core.config import Settings
from orchestrator.services.object_store import (
    CheckpointObjectStore,
    ObjectNotFoundError,
)
from trainer.checkpoint import CheckpointEntry, latest_entry, manifest_key

logger = logging.getLogger("orchestrator.checkpoints")


@dataclass(frozen=True, slots=True)
class CheckpointDownload:
    """A resolved, downloadable checkpoint."""

    entry: CheckpointEntry
    size_bytes: int | None


class _StoreAdapter:
    """Adapts :class:`CheckpointObjectStore` to trainer.checkpoint's Protocol.

    That Protocol expects ``get_bytes(key)`` positionally and raises its own
    ``ObjectNotFoundError``; ours is keyword-only and raises the orchestrator's.
    Adapting is a handful of lines and avoids either changing the trainer's
    contract — which runs on every peer — or reimplementing manifest parsing.
    """

    def __init__(self, store: CheckpointObjectStore) -> None:
        self._store = store

    def put_bytes(self, key: str, data: bytes) -> None:  # pragma: no cover
        raise NotImplementedError("the orchestrator never writes checkpoints")

    def get_bytes(self, key: str) -> bytes:
        from trainer.checkpoint import ObjectNotFoundError as TrainerNotFound

        try:
            return self._store.get_bytes(key=key)
        except ObjectNotFoundError as exc:
            raise TrainerNotFound(str(exc)) from exc


def latest_checkpoint_for(
    job_id: str, *, settings: Settings
) -> CheckpointDownload | None:
    """Resolve the newest checkpoint for ``job_id``, or None if there is none.

    Returns None both when the job never checkpointed and when the manifest
    exists but names no latest entry — from the caller's point of view those are
    the same answer ("nothing to download"), and distinguishing them in the API
    would expose storage internals to no benefit.

    Raises :class:`ObjectStoreError` if storage is unreachable, which the caller
    must report as a 503 rather than "no checkpoint": telling someone their model
    does not exist when the truth is that MinIO is down would send them looking
    for a training bug.
    """
    store = CheckpointObjectStore(settings)
    entry = latest_entry(_StoreAdapter(store), job_id)
    if entry is None:
        return None

    # Size is best-effort; a checkpoint that is listed but un-headable is still
    # downloadable, and an unknown size is reported as unknown rather than 0.
    return CheckpointDownload(entry=entry, size_bytes=store.head_size_bytes(key=entry.key))


def stream_checkpoint(key: str, *, settings: Settings):  # type: ignore[no-untyped-def]
    """Yield a checkpoint blob's bytes for streaming to a client."""
    return CheckpointObjectStore(settings).iter_object(key=key)


def suggested_filename(job_id: str, entry: CheckpointEntry) -> str:
    """A filename that identifies the model a week after it was downloaded.

    Without this every download lands as some opaque key basename, and three
    models in a downloads folder become indistinguishable.
    """
    return f"model-{job_id[:8]}-e{entry.epoch:03d}-s{entry.step:08d}.pt"


def manifest_object_key(job_id: str) -> str:
    """The manifest's key, re-exported so callers need not import the trainer."""
    return manifest_key(job_id)
