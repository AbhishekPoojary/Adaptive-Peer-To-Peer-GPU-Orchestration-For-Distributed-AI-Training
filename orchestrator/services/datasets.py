"""Dataset records: create, look up, retire (ADR-014).

Storage and validation live in :mod:`object_store` and :mod:`dataset_archive`.
This module owns the database row and the rules about which datasets may be
used, so the API layer never has to reason about either.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.models.dataset import Dataset
from orchestrator.services.dataset_archive import DatasetArchiveSummary

#: Bytes read per chunk when hashing an upload. 1 MiB keeps the read syscall
#: count low without holding a meaningful amount of the file in memory.
_HASH_CHUNK_BYTES = 1024 * 1024


class DatasetNameTakenError(Exception):
    """Another dataset already uses this name."""


def sha256_file(path: str) -> str:
    """Return the hex SHA-256 of a file, read in chunks.

    Chunked because the whole point of streaming the upload to disk was to avoid
    holding it in memory; hashing it with a single read would undo that.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def object_key_for(dataset_id: uuid.UUID) -> str:
    """Return the storage key for a dataset.

    Derived from the server-assigned id, never from the uploaded filename. A
    caller-controlled key would let a request name an object outside its own
    dataset, and a filename would collide the moment two people upload
    ``data.zip``.
    """
    return f"datasets/{dataset_id}/archive.zip"


async def create_dataset(
    session: AsyncSession,
    *,
    dataset_id: uuid.UUID,
    name: str,
    description: str | None,
    object_key: str,
    sha256: str,
    size_bytes: int,
    summary: DatasetArchiveSummary,
    created_by: str,
) -> Dataset:
    """Insert a dataset row. Caller commits.

    Uniqueness on ``name`` is enforced by the database index and the resulting
    IntegrityError translated here, rather than by a check-then-insert that two
    concurrent uploads could both pass.
    """
    dataset = Dataset(
        id=dataset_id,
        name=name,
        description=description,
        object_key=object_key,
        sha256=sha256,
        size_bytes=size_bytes,
        classes=summary.classes,
        train_images=summary.train.images,
        test_images=summary.test.images,
        per_class_counts={
            "train": summary.train.per_class,
            "test": summary.test.per_class,
        },
        created_by=created_by,
    )
    session.add(dataset)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise DatasetNameTakenError(
            f"a dataset named {name!r} already exists"
        ) from exc
    await session.refresh(dataset)
    return dataset


async def get_dataset(
    session: AsyncSession, *, dataset_id: uuid.UUID, include_deleted: bool = False
) -> Dataset | None:
    """Look up one dataset by id."""
    dataset = await session.get(Dataset, dataset_id)
    if dataset is None:
        return None
    if dataset.deleted_at is not None and not include_deleted:
        return None
    return dataset


async def list_datasets(
    session: AsyncSession, *, include_deleted: bool = False
) -> list[Dataset]:
    """Return datasets, newest first."""
    statement = select(Dataset).order_by(Dataset.created_at.desc())
    if not include_deleted:
        statement = statement.where(Dataset.deleted_at.is_(None))
    result = await session.execute(statement)
    return list(result.scalars().all())


async def soft_delete_dataset(session: AsyncSession, *, dataset_id: uuid.UUID) -> bool:
    """Mark a dataset deleted. Caller commits. Returns False if already gone.

    The row survives deliberately: a finished job records which dataset it
    trained on, and dropping the row would leave that job pointing at nothing.
    """
    result = await session.execute(
        update(Dataset)
        .where(Dataset.id == dataset_id, Dataset.deleted_at.is_(None))
        .values(deleted_at=func.now())
    )
    return bool(result.rowcount)


def dataset_spec_fields(dataset: Dataset) -> dict[str, Any]:
    """The dataset facts a trainer needs, as they are embedded in a job spec.

    Denormalized into the spec on purpose. A job's spec is the record of what it
    actually trained on, so it has to keep meaning the same thing after the
    dataset is renamed or retired — a foreign key alone would let history change
    underneath a completed run.
    """
    return {
        "dataset_id": str(dataset.id),
        "dataset_name": dataset.name,
        "dataset_sha256": dataset.sha256,
        "dataset_num_classes": len(dataset.classes),
    }
