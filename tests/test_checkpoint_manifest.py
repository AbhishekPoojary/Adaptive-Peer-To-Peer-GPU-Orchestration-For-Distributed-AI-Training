"""Checkpoint manifest atomicity + resume selection (ADR-006 addendum, M6).

Pure unit tests over ``trainer.checkpoint`` with an in-memory fake object store
(CONTRIBUTING.md #5: fakes live in tests/, named ``Fake*``) — no MinIO, no
boto3, no torch. They pin the two invariants the milestone calls out:

* a *failed blob upload never advances the manifest* (atomicity);
* a resume reads the *latest* checkpoint's real step/epoch from the manifest,
  and the exact bytes are retrievable at that key.
"""

from __future__ import annotations

import json

import pytest

from trainer.checkpoint import (
    ObjectNotFoundError,
    latest_entry,
    manifest_key,
    read_manifest,
    save_checkpoint,
)


class FakeObjectStore:
    """In-memory :class:`trainer.checkpoint.ObjectStore`. ``fail_blobs`` makes
    every checkpoint-*blob* PUT raise (manifest PUTs still succeed) so we can
    prove the ordering invariant without a real S3 backend."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_keys: list[str] = []
        self.fail_blobs = False

    def put_bytes(self, key: str, data: bytes) -> None:
        self.put_keys.append(key)
        if self.fail_blobs and key.startswith("checkpoints/"):
            raise RuntimeError("simulated blob upload failure")
        self.objects[key] = data

    def get_bytes(self, key: str) -> bytes:
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        return self.objects[key]


def _save(store: FakeObjectStore, *, step: int, epoch: int, blob: bytes):
    return save_checkpoint(
        store,
        job_id="job-1",
        blob=blob,
        step=step,
        epoch=epoch,
        world_size=1,
        lease_epoch=2,
        loss=1.0 / step,
    )


def test_first_attempt_has_no_manifest() -> None:
    store = FakeObjectStore()
    assert read_manifest(store, "job-1") is None
    assert latest_entry(store, "job-1") is None  # → trainer starts fresh


def test_save_then_latest_entry_round_trips_the_real_bytes() -> None:
    store = FakeObjectStore()
    entry = _save(store, step=100, epoch=2, blob=b"real-checkpoint-bytes-100")
    latest = latest_entry(store, "job-1")
    assert latest is not None
    assert latest.step == 100
    assert latest.epoch == 2
    assert latest.key == entry.key
    # The exact blob is retrievable at the manifest's key — resume can load it.
    assert store.get_bytes(latest.key) == b"real-checkpoint-bytes-100"


def test_blob_is_put_before_the_manifest() -> None:
    store = FakeObjectStore()
    _save(store, step=100, epoch=2, blob=b"x")
    # Ordering invariant: the blob key is PUT strictly before the manifest key.
    blob_puts = [i for i, k in enumerate(store.put_keys) if k.startswith("checkpoints/")]
    manifest_puts = [i for i, k in enumerate(store.put_keys) if k == manifest_key("job-1")]
    assert blob_puts and manifest_puts
    assert max(blob_puts) < min(manifest_puts)


def test_latest_tracks_the_newest_of_many_checkpoints() -> None:
    store = FakeObjectStore()
    _save(store, step=50, epoch=1, blob=b"a")
    _save(store, step=100, epoch=2, blob=b"b")
    _save(store, step=150, epoch=3, blob=b"c")
    latest = latest_entry(store, "job-1")
    assert latest is not None
    assert (latest.step, latest.epoch) == (150, 3)
    manifest = read_manifest(store, "job-1")
    assert manifest is not None
    # Append-only, ordered oldest→newest; latest == checkpoints[-1].
    steps = [c["step"] for c in manifest["checkpoints"]]
    assert steps == [50, 100, 150]
    assert manifest["latest"] == manifest["checkpoints"][-1]


def test_failed_blob_upload_never_advances_the_manifest() -> None:
    """THE atomicity invariant: if the checkpoint blob PUT fails, the manifest
    is untouched — it keeps pointing at the previous good checkpoint, never a
    partial one."""
    store = FakeObjectStore()
    _save(store, step=100, epoch=2, blob=b"good-100")
    manifest_before = store.get_bytes(manifest_key("job-1"))

    store.fail_blobs = True
    with pytest.raises(RuntimeError, match="simulated blob upload failure"):
        _save(store, step=200, epoch=3, blob=b"partial-200")

    # Manifest bytes are byte-for-byte unchanged...
    assert store.get_bytes(manifest_key("job-1")) == manifest_before
    # ...still points at step 100 (the last good checkpoint)...
    latest = latest_entry(store, "job-1")
    assert latest is not None
    assert latest.step == 100
    # ...and no step-200 blob was ever recorded.
    assert not any("s00000200" in k for k in store.objects)


def test_manifest_is_valid_json_with_schema_and_job_id() -> None:
    store = FakeObjectStore()
    _save(store, step=100, epoch=2, blob=b"x")
    manifest = json.loads(store.get_bytes(manifest_key("job-1")).decode("utf-8"))
    assert manifest["schema"] == 1
    assert manifest["job_id"] == "job-1"
    assert "updated_utc" in manifest
