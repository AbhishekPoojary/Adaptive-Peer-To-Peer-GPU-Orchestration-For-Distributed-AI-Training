"""Schemas for the dataset surface (ADR-014)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

#: Dataset names appear in job specs and object keys, so they are constrained to
#: something unambiguous and safe to render: letters, digits, dash, underscore,
#: dot. No slashes (they would read as path separators) and no whitespace.
DATASET_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$"


class DatasetOut(BaseModel):
    """A dataset as returned to clients.

    Carries no ``object_key``: where the archive sits in the bucket is the
    server's business, and a client that knew the key would be one signed URL
    away from reading data it was not handed.
    """

    id: uuid.UUID
    name: str
    description: str | None
    sha256: str
    size_bytes: int
    classes: list[str]
    num_classes: int
    train_images: int
    test_images: int
    #: split -> class -> count, so an imbalanced upload is visible before
    #: someone spends an hour training on it.
    per_class_counts: dict[str, dict[str, int]]
    created_by: str
    created_at: datetime


class DatasetListResponse(BaseModel):
    """Body of GET /datasets."""

    datasets: list[DatasetOut]


class DatasetUploadAccepted(BaseModel):
    """Body of a successful POST /datasets.

    Returns the full record rather than just an id: the uploader's next question
    is always "did it read my folders the way I meant?", and the class list and
    counts answer it without a second request.
    """

    dataset: DatasetOut
    #: Plain-language confirmation of what was stored, e.g.
    #: "3 classes, 900 training and 300 test images".
    summary: str
    #: What the server had to change to make the archive usable -- folders
    #: renamed onto train/ and test/, unlabelled images skipped, a held-out
    #: split carved. Empty when the archive already matched the required
    #: layout, which is the only case where nothing was decided on the
    #: uploader's behalf. Shown rather than buried: a rearrangement the
    #: uploader never sees is indistinguishable from the server guessing.
    layout_notes: list[str] = []


class DatasetNameCheck(BaseModel):
    """Query params for name availability, used by the upload form."""

    name: str = Field(pattern=DATASET_NAME_PATTERN)
