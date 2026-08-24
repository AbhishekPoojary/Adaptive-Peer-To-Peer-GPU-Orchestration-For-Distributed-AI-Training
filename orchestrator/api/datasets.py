"""Dataset endpoints (ADR-014).

- ``POST /datasets``: upload a validated image-classification archive.
- ``GET /datasets``: list what is available to train on.
- ``GET /datasets/{id}``: one dataset's detail.
- ``DELETE /datasets/{id}``: retire a dataset (soft delete + object removal).

Uploading is ADMIN-only. A dataset is executed-against on other people's
machines: every peer that claims a job fetches and extracts it. That is the same
class of privilege as enrolling a node, which ADR-012 already put behind ADMIN,
so it sits behind the same gate rather than a new one.

Reading is open to any authenticated user, because an OPERATOR has to see the
list to submit a job at all.
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import get_settings_dep, require_admin_user, require_user
from orchestrator.core.config import Settings
from orchestrator.core.db import get_session
from orchestrator.models.dataset import Dataset
from orchestrator.models.user import User
from orchestrator.schemas.dataset import (
    DATASET_NAME_PATTERN,
    DatasetListResponse,
    DatasetOut,
    DatasetUploadAccepted,
)
from orchestrator.services.dataset_archive import (
    DatasetArchiveError,
    summarize_dataset_archive,
)
from orchestrator.services.datasets import (
    DatasetNameTakenError,
    create_dataset,
    get_dataset,
    list_datasets,
    object_key_for,
    sha256_file,
    soft_delete_dataset,
)
from orchestrator.services.object_store import DatasetObjectStore, ObjectStoreError

logger = logging.getLogger("orchestrator.datasets")

router = APIRouter(prefix="/datasets", tags=["datasets"])

#: Read size when streaming the upload to disk.
_UPLOAD_CHUNK_BYTES = 1024 * 1024


def _dataset_out(dataset: Dataset) -> DatasetOut:
    return DatasetOut(
        id=dataset.id,
        name=dataset.name,
        description=dataset.description,
        sha256=dataset.sha256,
        size_bytes=dataset.size_bytes,
        classes=list(dataset.classes),
        num_classes=len(dataset.classes),
        train_images=dataset.train_images,
        test_images=dataset.test_images,
        per_class_counts=dict(dataset.per_class_counts),
        created_by=dataset.created_by,
        created_at=dataset.created_at,
    )


async def _stream_to_temp(upload: UploadFile, *, max_bytes: int) -> tuple[str, int]:
    """Write the upload to a temp file, refusing to exceed ``max_bytes``.

    The cap is enforced *while writing* rather than from Content-Length, which a
    client controls and can simply lie about. Going over aborts mid-stream and
    deletes the partial file, so an oversized upload costs the disk it had
    written so far and nothing more.
    """
    handle, path = tempfile.mkstemp(suffix=".zip", prefix="dataset-upload-")
    written = 0
    try:
        with os.fdopen(handle, "wb") as sink:
            while chunk := await upload.read(_UPLOAD_CHUNK_BYTES):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=(
                            "dataset archive is larger than "
                            f"{max_bytes // (1024 * 1024)} MiB"
                        ),
                    )
                sink.write(chunk)
    except BaseException:
        os.unlink(path)
        raise
    return path, written


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=DatasetUploadAccepted,
    dependencies=[Depends(require_admin_user)],
)
async def upload_dataset(
    name: str = Form(pattern=DATASET_NAME_PATTERN),
    description: str | None = Form(default=None, max_length=1024),
    file: UploadFile = File(...),
    user: User = Depends(require_admin_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> DatasetUploadAccepted:
    """Validate and store an image-classification dataset.

    The archive is streamed to disk, inspected without being decompressed, and
    only then uploaded to object storage — so nothing reaches the bucket that has
    not already been proved to be a plain tree of images.
    """
    archive_path, size_bytes = await _stream_to_temp(
        file, max_bytes=settings.dataset_max_upload_bytes
    )
    try:
        try:
            summary = summarize_dataset_archive(
                archive_path,
                max_files=settings.dataset_max_files,
                max_uncompressed_bytes=settings.dataset_max_uncompressed_bytes,
                min_classes=settings.dataset_min_classes,
                max_classes=settings.dataset_max_classes,
            )
        except DatasetArchiveError as exc:
            # Specific on purpose: the person holding the file is the only one
            # who can fix it, and "invalid archive" tells them nothing about
            # which of a dozen requirements they missed.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc

        digest = sha256_file(archive_path)
        dataset_id = uuid.uuid4()
        key = object_key_for(dataset_id)

        # Insert first, upload second. The unique index on name is what actually
        # decides a race between two uploads, and losing that race after pushing
        # gigabytes into the bucket would leave an orphaned object behind.
        try:
            dataset = await create_dataset(
                session,
                dataset_id=dataset_id,
                name=name,
                description=description,
                object_key=key,
                sha256=digest,
                size_bytes=size_bytes,
                summary=summary,
                created_by=user.username,
            )
        except DatasetNameTakenError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc

        store = DatasetObjectStore(settings)
        try:
            store.ensure_bucket()
            store.upload_file(local_path=archive_path, key=key)
        except ObjectStoreError as exc:
            # The row is not committed, so nothing references the object that
            # never arrived. 503 rather than 500: storage being down is an
            # operational state, not a bug in the request.
            await session.rollback()
            logger.error("dataset upload to object storage failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "the dataset was valid but object storage is unavailable; "
                    "try again once it is back"
                ),
            ) from exc

        await session.commit()
        await session.refresh(dataset)
    finally:
        os.unlink(archive_path)

    logger.info(
        "dataset %s (%s) uploaded by %s: %d classes, %d train / %d test images",
        dataset.name,
        dataset.id,
        user.username,
        len(summary.classes),
        summary.train.images,
        summary.test.images,
    )
    return DatasetUploadAccepted(
        dataset=_dataset_out(dataset),
        summary=(
            f"{len(summary.classes)} classes, "
            f"{summary.train.images} training and {summary.test.images} test images"
        ),
    )


@router.get("", response_model=DatasetListResponse)
async def list_datasets_endpoint(
    _user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> DatasetListResponse:
    """List datasets available to train on, newest first."""
    datasets = await list_datasets(session)
    return DatasetListResponse(datasets=[_dataset_out(d) for d in datasets])


@router.get("/{dataset_id}", response_model=DatasetOut)
async def get_dataset_endpoint(
    dataset_id: uuid.UUID,
    _user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> DatasetOut:
    """Return one dataset's detail."""
    dataset = await get_dataset(session, dataset_id=dataset_id)
    if dataset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown dataset"
        )
    return _dataset_out(dataset)


@router.delete(
    "/{dataset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # 204 carries no body. FastAPI infers response_model from the handler's
    # return annotation, and `-> None` yields NoneType, which is truthy enough to
    # trip its "status code must not have a response body" assertion at import
    # time. Both of these are needed to say "there is genuinely no body".
    response_class=Response,
    response_model=None,
)
async def delete_dataset_endpoint(
    dataset_id: uuid.UUID,
    _admin: User = Depends(require_admin_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> None:
    """Retire a dataset: hide it from new jobs and remove the stored archive.

    The row is kept. A finished job records which dataset it trained on, and
    dropping the row would turn that record into an unanswerable question.
    """
    dataset = await get_dataset(session, dataset_id=dataset_id)
    if dataset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown dataset"
        )

    key = dataset.object_key
    await soft_delete_dataset(session, dataset_id=dataset_id)
    await session.commit()

    # After the commit: the operator's intent is already durable, so a storage
    # hiccup here must not resurrect a dataset they have said should be gone.
    DatasetObjectStore(settings).delete_object(key=key)
