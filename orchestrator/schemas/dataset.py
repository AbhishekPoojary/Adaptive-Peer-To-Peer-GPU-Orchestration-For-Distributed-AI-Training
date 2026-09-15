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


class UploadLimits(BaseModel):
    """What a client needs before it can prepare an upload.

    Served rather than duplicated in the dashboard, so the numbers a client
    plans around are the ones the server will actually enforce.
    """

    chunk_bytes: int
    max_upload_bytes: int
    #: Resolution the trainer reduces every custom-dataset image to. A client
    #: that shrinks images to this before uploading loses nothing, because
    #: anything larger is discarded on arrival.
    image_size: int


class UploadSessionCreate(BaseModel):
    """Body of POST /datasets/uploads — opening a chunked upload.

    The name and description are taken now rather than at completion so a
    clash is discovered before the bytes are sent, not after.
    """

    name: str = Field(pattern=DATASET_NAME_PATTERN)
    description: str | None = Field(default=None, max_length=1024)
    #: Size of the archive about to be sent. Checked against the upload ceiling
    #: immediately, and against what actually arrives before anything is stored.
    total_bytes: int = Field(gt=0)
    #: Changes the client made to the archive before sending it — currently
    #: only "images were resized". Recorded on the dataset beside the server's
    #: own layout notes, for the same reason: the stored archive is then not
    #: byte-identical to the file someone chose, and a reader deserves to know
    #: which transformations stand between the two.
    #:
    #: Descriptive, not load-bearing. The server re-validates everything it
    #: stores regardless of what is claimed here.
    client_notes: list[str] = Field(default_factory=list, max_length=8)


class UploadSessionStatus(BaseModel):
    """An upload in progress.

    ``received`` is what makes this resumable: a client that was interrupted
    asks where it got to and sends only what is missing, rather than starting a
    350 MB archive again because one chunk failed.
    """

    upload_id: uuid.UUID
    #: Chunk size the server expects. The client does not choose it — a chunk
    #: large enough to be cut off in transit would defeat the whole mechanism.
    chunk_bytes: int
    total_chunks: int
    total_bytes: int
    received: list[int]

    @property
    def is_complete(self) -> bool:
        return len(self.received) == self.total_chunks


class DatasetNameCheck(BaseModel):
    """Query params for name availability, used by the upload form."""

    name: str = Field(pattern=DATASET_NAME_PATTERN)
