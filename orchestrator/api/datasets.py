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

import contextlib
import logging
import os
import tempfile
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
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
    UploadLimits,
    UploadSessionCreate,
    UploadSessionStatus,
)
from orchestrator.services.dataset_archive import (
    DatasetArchiveError,
    DatasetArchiveSummary,
    summarize_dataset_archive,
)
from orchestrator.services.dataset_layout import plan_normalization, rewrite_archive
from orchestrator.services.datasets import (
    DatasetNameTakenError,
    create_dataset,
    get_dataset,
    list_datasets,
    name_conflict,
    object_key_for,
    sha256_file,
    soft_delete_dataset,
)
from orchestrator.services.object_store import DatasetObjectStore, ObjectStoreError
from orchestrator.services.upload_sessions import (
    UploadSession,
    UploadSessionError,
    assemble,
    create_session,
    discard_session,
    load_session,
    received_chunks,
    store_chunk,
    sweep_expired,
)
from trainer.dataset_spec import CUSTOM_IMAGE_SIZE

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


#: Longest description the column holds, mirroring ``Dataset.description``.
_DESCRIPTION_LIMIT = 1024


def _prepare_archive(
    archive_path: str, settings: Settings, scratch: list[str]
) -> tuple[str, DatasetArchiveSummary, list[str]]:
    """Return the archive to store, its summary, and what had to change.

    A conformant archive is stored exactly as uploaded and the notes are empty
    -- this path is byte-for-byte what it was before layout normalisation
    existed. Only a *rejected* archive is rearranged, and the rearranged result
    is then put through the same validator, so nothing is stored that the
    validator would not have accepted on its own.

    Any temp file created here is appended to ``scratch`` for the caller to
    remove; returning the path alone would leave the original leaking whenever
    normalisation produced a second one.
    """
    limits = {
        "max_files": settings.dataset_max_files,
        "max_uncompressed_bytes": settings.dataset_max_uncompressed_bytes,
        "min_classes": settings.dataset_min_classes,
        "max_classes": settings.dataset_max_classes,
    }
    try:
        return archive_path, summarize_dataset_archive(archive_path, **limits), []
    except DatasetArchiveError as exc:
        if not settings.dataset_normalize_layout:
            raise
        # Rebound deliberately: Python unbinds an `except ... as` name when the
        # block ends, and this complaint has to outlive it to be re-raised.
        original = exc

    # Planning raises on the unsafe entries the validator refuses, and those
    # must surface as themselves rather than as "could not be normalised" --
    # the uploader of a zip-slip archive deserves to be told which entry.
    plan = plan_normalization(
        archive_path,
        max_files=settings.dataset_max_files,
        max_uncompressed_bytes=settings.dataset_max_uncompressed_bytes,
        test_fraction=settings.dataset_autosplit_fraction,
    )
    if plan is None:
        raise original

    handle, rewritten = tempfile.mkstemp(suffix=".zip", prefix="dataset-normalized-")
    os.close(handle)
    scratch.append(rewritten)
    rewrite_archive(
        archive_path,
        rewritten,
        plan,
        max_uncompressed_bytes=settings.dataset_max_uncompressed_bytes,
    )
    # The rewrite is not trusted on its own account: if the rearranged archive
    # still fails, the uploader hears the original complaint about the archive
    # they actually sent, not a second one about a file they never saw.
    try:
        summary = summarize_dataset_archive(rewritten, **limits)
    except DatasetArchiveError:
        raise original from None
    return rewritten, summary, plan.notes


def _describe_layout_changes(description: str | None, notes: list[str]) -> str | None:
    """Fold ``notes`` into the stored description.

    ``dataset_archive`` refuses to carve a held-out split *silently*, and it is
    right: an accuracy figure whose test set was chosen by a machine, invisibly,
    is a number the reader cannot weigh. Recording the change on the dataset
    itself is what makes the rearrangement honest -- anyone reading a result a
    month later sees how the split it was measured against came to exist.
    """
    if not notes:
        return description
    appendix = "[layout] " + "; ".join(notes)
    if description:
        room = _DESCRIPTION_LIMIT - len(appendix) - 2
        if room <= 0:
            return appendix[:_DESCRIPTION_LIMIT]
        return f"{description[:room]}\n\n{appendix}"
    return appendix[:_DESCRIPTION_LIMIT]


async def _validate_and_store(
    upload_path: str,
    *,
    name: str,
    description: str | None,
    user: User,
    session: AsyncSession,
    settings: Settings,
    client_notes: list[str] | None = None,
) -> DatasetUploadAccepted:
    """Validate the archive at ``upload_path``, store it, and record the dataset.

    Shared by both ways an archive can arrive — one request, or many chunks
    assembled into one file. Everything after "there is a complete archive on
    disk" is identical, and writing it twice would be an invitation for the two
    paths to disagree about what a dataset is.

    Takes ownership of ``upload_path``: it is deleted before this returns, along
    with anything normalisation created alongside it.
    """
    scratch = [upload_path]
    try:
        try:
            archive_path, summary, notes = _prepare_archive(
                upload_path, settings, scratch
            )
        except DatasetArchiveError as exc:
            # Specific on purpose: the person holding the file is the only one
            # who can fix it, and "invalid archive" tells them nothing about
            # which of a dozen requirements they missed.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc

        # The client's account of what it changed goes first: it happened first,
        # and it describes the file the server was given rather than what the
        # server then did to it.
        notes = [*(client_notes or []), *notes]

        # Of the archive that is actually stored, which is not the uploaded one
        # when the layout had to be rearranged. The peer verifies this digest
        # against the bytes it downloads, so it has to describe those bytes.
        size_bytes = os.path.getsize(archive_path)
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
                description=_describe_layout_changes(description, notes),
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
        for path in scratch:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)

    if notes:
        logger.info(
            "dataset %s: archive layout normalised (%s)", dataset.name, "; ".join(notes)
        )
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
        layout_notes=notes,
    )


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
    """Validate and store an image-classification dataset in one request.

    The archive is streamed to disk, inspected without being decompressed, and
    only then uploaded to object storage — so nothing reaches the bucket that has
    not already been proved to be a plain tree of images.

    Fine over a LAN, where 350 MB takes a couple of seconds. For an uploader
    reaching this through a tunnel that cuts long transfers off, see the chunked
    routes below.
    """
    upload_path, _upload_bytes = await _stream_to_temp(
        file, max_bytes=settings.dataset_max_upload_bytes
    )
    return await _validate_and_store(
        upload_path,
        name=name,
        description=description,
        user=user,
        session=session,
        settings=settings,
    )


# --- Chunked, resumable upload ------------------------------------------------
#
# Declared before the /{dataset_id} routes so the shapes here are read first.
# They do not actually collide -- "uploads/<id>" is two segments and
# "{dataset_id}" is one -- but relying on that is a trap for whoever adds
# /datasets/{dataset_id}/something next.


def _upload_root(settings: Settings) -> Path:
    """Directory holding partial uploads, created on first use."""
    base = settings.dataset_upload_scratch_dir or os.path.join(
        tempfile.gettempdir(), "gpu-orchestrator-uploads"
    )
    root = Path(base)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _owned_session(root: Path, upload_id: uuid.UUID, user: User) -> UploadSession:
    """Load a session belonging to ``user``, or 404.

    Someone else's session answers exactly as a non-existent one does. A
    distinguishable "that exists but is not yours" would turn this into a way
    to enumerate what other people are uploading and under what names.
    """
    upload = load_session(root, upload_id)
    if upload is None or upload.owner != user.username:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown upload"
        )
    return upload


def _status_of(upload: UploadSession) -> UploadSessionStatus:
    return UploadSessionStatus(
        upload_id=upload.upload_id,
        chunk_bytes=upload.chunk_bytes,
        total_chunks=upload.total_chunks,
        total_bytes=upload.total_bytes,
        received=received_chunks(upload),
    )


@router.get("/upload-limits", response_model=UploadLimits)
async def upload_limits(
    _user: User = Depends(require_user),
    settings: Settings = Depends(get_settings_dep),
) -> UploadLimits:
    """What a client needs to know before it starts preparing an upload.

    ``image_size`` is the resolution the trainer reduces every custom-dataset
    image to. The dashboard shrinks archives to it before sending, which turns
    a 363 MB upload into 55 MB with no effect on training — the detail beyond
    it is discarded on arrival either way.

    Served rather than hardcoded in the dashboard so there is one copy of the
    number. Two that drifted apart would not fail loudly: images would be
    shrunk to one size and resized up to another, and the only symptom would be
    a slightly disappointing accuracy with no visible cause.
    """
    return UploadLimits(
        chunk_bytes=settings.dataset_upload_chunk_bytes,
        max_upload_bytes=settings.dataset_max_upload_bytes,
        image_size=CUSTOM_IMAGE_SIZE,
    )


@router.post(
    "/uploads",
    status_code=status.HTTP_201_CREATED,
    response_model=UploadSessionStatus,
)
async def open_upload_session(
    body: UploadSessionCreate,
    user: User = Depends(require_admin_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> UploadSessionStatus:
    """Open a chunked upload and say how to send it.

    For an uploader whose route to here cuts long transfers off -- the public
    tunnel does so after a minute or two -- a single request carrying a large
    archive cannot succeed no matter how often it is retried. Splitting it means
    no request runs long enough to be cut, and a chunk that fails anyway costs
    one chunk rather than the whole file.

    The name is checked now, not at the end. It is the same refusal either way,
    and delivering it after the bytes have gone up is delivering it at the most
    expensive possible moment.
    """
    root = _upload_root(settings)
    # Reclaiming abandoned chunks at the moment someone asks for more disk needs
    # no scheduler and no background task to keep alive.
    swept = sweep_expired(root, ttl_seconds=settings.dataset_upload_session_ttl_seconds)
    if swept:
        logger.info("swept %d expired upload session(s)", swept)

    conflict = await name_conflict(session, body.name)
    if conflict is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=conflict)

    try:
        upload = create_session(
            root,
            name=body.name,
            description=body.description,
            client_notes=body.client_notes,
            total_bytes=body.total_bytes,
            chunk_bytes=settings.dataset_upload_chunk_bytes,
            owner=user.username,
            max_upload_bytes=settings.dataset_max_upload_bytes,
        )
    except UploadSessionError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc

    logger.info(
        "upload %s opened by %s: %d bytes in %d chunk(s)",
        upload.upload_id,
        user.username,
        upload.total_bytes,
        upload.total_chunks,
    )
    return _status_of(upload)


@router.get("/uploads/{upload_id}", response_model=UploadSessionStatus)
async def upload_session_status(
    upload_id: uuid.UUID,
    user: User = Depends(require_admin_user),
    settings: Settings = Depends(get_settings_dep),
) -> UploadSessionStatus:
    """What has arrived so far -- the basis for resuming an interrupted upload."""
    return _status_of(_owned_session(_upload_root(settings), upload_id, user))


@router.put(
    "/uploads/{upload_id}/chunks/{index}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
async def put_upload_chunk(
    upload_id: uuid.UUID,
    index: int,
    request: Request,
    user: User = Depends(require_admin_user),
    settings: Settings = Depends(get_settings_dep),
) -> None:
    """Store one chunk. Sending the same index again replaces it.

    Idempotent on purpose: a chunk whose request died partway is exactly the
    case this endpoint exists to survive, and the client's only sane response is
    to send it again.
    """
    upload = _owned_session(_upload_root(settings), upload_id, user)

    # Read with the ceiling applied as it arrives rather than from
    # Content-Length, which the client controls and can simply misstate.
    buffer = bytearray()
    async for block in request.stream():
        buffer.extend(block)
        if len(buffer) > upload.chunk_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"a chunk may not exceed {upload.chunk_bytes} bytes",
            )

    try:
        store_chunk(upload, index, bytes(buffer))
    except UploadSessionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.post(
    "/uploads/{upload_id}/complete",
    status_code=status.HTTP_201_CREATED,
    response_model=DatasetUploadAccepted,
)
async def complete_upload_session(
    upload_id: uuid.UUID,
    user: User = Depends(require_admin_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> DatasetUploadAccepted:
    """Assemble the chunks and store the result.

    From here on this is the single-shot path exactly: the same validation, the
    same layout normalisation, the same record. How the bytes arrived stops
    mattering once they are one file on disk.
    """
    upload = _owned_session(_upload_root(settings), upload_id, user)

    handle, assembled = tempfile.mkstemp(suffix=".zip", prefix="dataset-assembled-")
    os.close(handle)
    try:
        assemble(upload, assembled)
    except UploadSessionError as exc:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(assembled)
        # 409, not 422: nothing is wrong with the archive, the transfer is
        # simply not finished. The client can fill the gaps and complete again.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    try:
        result = await _validate_and_store(
            assembled,
            name=upload.name,
            description=upload.description,
            user=user,
            session=session,
            settings=settings,
            client_notes=upload.client_notes,
        )
    except HTTPException as exc:
        # A 4xx is a verdict on the archive itself, and re-sending the same
        # bytes would earn the same verdict -- so the chunks go. A 5xx is
        # storage or the database having a bad moment, so they are kept and the
        # uploader can complete again without re-sending anything.
        if exc.status_code < 500:
            discard_session(upload)
        raise
    discard_session(upload)
    return result


@router.delete(
    "/uploads/{upload_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
)
async def abandon_upload_session(
    upload_id: uuid.UUID,
    user: User = Depends(require_admin_user),
    settings: Settings = Depends(get_settings_dep),
) -> None:
    """Give up on an upload and release its chunks straight away.

    Expiry would get there eventually; a client that knows it has stopped should
    not make the disk wait for it.
    """
    discard_session(_owned_session(_upload_root(settings), upload_id, user))


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
