"""Uploaded training datasets (ADR-014).

A row here is a record of an archive that has been validated and stored in
object storage. It is deliberately *not* the data: the images live in MinIO
under :attr:`Dataset.object_key`, because a peer needs to fetch them directly
and streaming gigabytes through the orchestrator would make the control plane a
bottleneck for the one operation that is guaranteed to be large.

The columns here are the ones a peer needs to fetch and trust the archive
(``object_key``, ``sha256``) plus the ones a human needs to tell two uploads
apart (name, counts, classes).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from orchestrator.core.db import Base


class Dataset(Base):
    """A validated, uploaded image-classification dataset."""

    __tablename__ = "datasets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Human-chosen, unique, and used in the job spec's audit trail. Unique so a
    # job that says it trained on "flowers" cannot be ambiguous a month later.
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # Where the archive lives in the datasets bucket. Assigned by the server, so
    # a caller can never steer a read at another object.
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    # Hex SHA-256 of the exact bytes stored. The peer recomputes this after
    # downloading and refuses to extract on a mismatch, which is what makes
    # "validated at upload" mean anything at the far end (ADR-014 §4).
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Sorted class names. The trainer derives its output layer width from
    # len(classes), so this is load-bearing, not decoration.
    classes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    train_images: Mapped[int] = mapped_column(Integer, nullable=False)
    test_images: Mapped[int] = mapped_column(Integer, nullable=False)
    # split -> class -> count. Shown in the UI so a wildly imbalanced upload is
    # visible before someone spends an hour training on it.
    per_class_counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # Taken from the uploader's token, never from the request body — the same
    # rule job attribution follows (ADR-012 §4).
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Soft delete. Jobs record which dataset they trained on, and hard-deleting a
    # row would leave a completed job pointing at nothing — turning a real
    # training record into an unanswerable question. A deleted dataset is hidden
    # from the picker and refused for new jobs; the object itself is removed.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def is_available(self) -> bool:
        """True iff this dataset may be selected for a new job."""
        return self.deleted_at is None
