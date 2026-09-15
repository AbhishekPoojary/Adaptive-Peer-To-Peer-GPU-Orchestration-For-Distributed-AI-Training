"""Resumable chunked dataset uploads (ADR-014, amended).

A single ``POST /datasets`` carrying the whole archive is the right shape when
the uploader and the orchestrator are on the same network. It stops being the
right shape the moment the dashboard is shared through a Cloudflare quick
tunnel, which is how a collaborator on another network reaches it: the tunnel
cuts a transfer off after a minute or two, so an upload's chance of finishing
depends on the uploader's upstream rather than on anything either end decides.
Measured on the link this was built for, a 346 MB archive died after 34 MB
while the same file took 2.5s over the LAN.

Splitting the archive across many short requests removes the dependency. No
single request runs long enough to be cut off, and a request that fails anyway
costs one chunk instead of the whole file.

State lives on disk, next to the bytes
--------------------------------------
Each session is a directory holding its chunks and a ``manifest.json``::

    <root>/<upload_id>/manifest.json
    <root>/<upload_id>/000000.part

In-memory state would be simpler until the orchestrator restarts mid-upload,
at which point the chunks on disk would be unreferenced and the client would
be told its session had never existed. Keeping the manifest beside the chunks
means one thing to find, one thing to expire, and one thing to delete.

What this module refuses
------------------------
An upload session is disk the caller has not yet justified: it is accepted
before anything about the archive has been proved. So the ceilings are checked
on the way *in*, not at the end --

- the declared total against ``dataset_max_upload_bytes``, at creation;
- the bytes actually stored against that declared total, on every chunk, so a
  client that lies about its size in one direction cannot spend disk in the
  other;
- the chunk index against the count the session was opened with;
- and the assembled size against the declared total, before a single byte is
  handed on to validation.

Sessions are owned by the account that opened them and expire, because a
client that vanishes half way through must not leave its chunks behind for
good.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

#: Manifest filename inside a session directory.
_MANIFEST_NAME = "manifest.json"

#: Suffix for a stored chunk. Named by index so the assembly order is the
#: filename order and nothing has to be recorded to reproduce it.
_CHUNK_SUFFIX = ".part"

#: Read size when assembling chunks into the final archive.
_ASSEMBLE_CHUNK_BYTES = 1024 * 1024


class UploadSessionError(Exception):
    """The session cannot be used as asked. The message reaches the uploader."""


@dataclass(frozen=True, slots=True)
class UploadSession:
    """An upload in progress, reconstructed from its manifest."""

    upload_id: uuid.UUID
    name: str
    description: str | None
    #: What the client changed before sending, carried through to the record.
    client_notes: list[str]
    total_bytes: int
    chunk_bytes: int
    total_chunks: int
    owner: str
    created_at: float
    directory: Path


def _session_dir(root: Path, upload_id: uuid.UUID) -> Path:
    # str(uuid.UUID) is hex and dashes whatever was parsed, so this cannot
    # contain a separator even if the caller sent something exotic.
    return root / str(upload_id)


def _chunk_path(session: UploadSession, index: int) -> Path:
    return session.directory / f"{index:06d}{_CHUNK_SUFFIX}"


def expected_chunk_count(total_bytes: int, chunk_bytes: int) -> int:
    """How many chunks a file of ``total_bytes`` needs. Zero bytes means zero."""
    if chunk_bytes <= 0:
        raise UploadSessionError("chunk size must be positive")
    return (total_bytes + chunk_bytes - 1) // chunk_bytes


def create_session(
    root: Path,
    *,
    name: str,
    description: str | None,
    client_notes: list[str],
    total_bytes: int,
    chunk_bytes: int,
    owner: str,
    max_upload_bytes: int,
) -> UploadSession:
    """Open a session and return it. Raises :class:`UploadSessionError`.

    The size ceiling is enforced here rather than at assembly: a session that
    could never be completed should not be allowed to spend disk first.
    """
    if total_bytes <= 0:
        raise UploadSessionError("an upload needs a positive size")
    if total_bytes > max_upload_bytes:
        raise UploadSessionError(
            f"dataset archive is larger than {max_upload_bytes // (1024 * 1024)} MiB"
        )

    upload_id = uuid.uuid4()
    directory = _session_dir(root, upload_id)
    directory.mkdir(parents=True, exist_ok=False)

    session = UploadSession(
        upload_id=upload_id,
        name=name,
        description=description,
        client_notes=list(client_notes),
        total_bytes=total_bytes,
        chunk_bytes=chunk_bytes,
        total_chunks=expected_chunk_count(total_bytes, chunk_bytes),
        owner=owner,
        created_at=time.time(),
        directory=directory,
    )
    (directory / _MANIFEST_NAME).write_text(
        json.dumps(
            {
                "upload_id": str(session.upload_id),
                "name": session.name,
                "description": session.description,
                "client_notes": session.client_notes,
                "total_bytes": session.total_bytes,
                "chunk_bytes": session.chunk_bytes,
                "total_chunks": session.total_chunks,
                "owner": session.owner,
                "created_at": session.created_at,
            }
        ),
        encoding="utf-8",
    )
    return session


def load_session(root: Path, upload_id: uuid.UUID) -> UploadSession | None:
    """Read a session back, or ``None`` if there is no such session."""
    directory = _session_dir(root, upload_id)
    manifest = directory / _MANIFEST_NAME
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A directory with no readable manifest is wreckage, not a session --
        # a half-written create, or something else entirely under the root.
        return None
    return UploadSession(
        upload_id=uuid.UUID(raw["upload_id"]),
        name=raw["name"],
        client_notes=list(raw.get("client_notes") or []),
        description=raw["description"],
        total_bytes=int(raw["total_bytes"]),
        chunk_bytes=int(raw["chunk_bytes"]),
        total_chunks=int(raw["total_chunks"]),
        owner=raw["owner"],
        created_at=float(raw["created_at"]),
        directory=directory,
    )


def received_chunks(session: UploadSession) -> list[int]:
    """Indices already stored, ascending. This is what makes a resume possible."""
    indices = []
    for entry in session.directory.iterdir():
        if entry.name.endswith(_CHUNK_SUFFIX):
            try:
                indices.append(int(entry.name[: -len(_CHUNK_SUFFIX)]))
            except ValueError:
                continue
    return sorted(indices)


def stored_bytes(session: UploadSession, *, excluding: int | None = None) -> int:
    """Bytes currently held by the session, optionally ignoring one index."""
    total = 0
    for index in received_chunks(session):
        if index == excluding:
            continue
        total += _chunk_path(session, index).stat().st_size
    return total


def store_chunk(session: UploadSession, index: int, data: bytes) -> None:
    """Write one chunk, replacing any previous copy of the same index.

    Replacing rather than refusing is what makes a retry safe: a chunk whose
    request died halfway may have left a short file behind, and the client's
    only sane response is to send it again.
    """
    if index < 0 or index >= session.total_chunks:
        raise UploadSessionError(
            f"chunk {index} is outside this upload, which has "
            f"{session.total_chunks} chunk(s)"
        )
    if len(data) > session.chunk_bytes:
        raise UploadSessionError(
            f"chunk {index} is larger than the {session.chunk_bytes}-byte chunk "
            "size this upload was opened with"
        )

    # Counted with this index's previous copy discounted, so re-sending a chunk
    # cannot inch the session over its declared size one retry at a time.
    if stored_bytes(session, excluding=index) + len(data) > session.total_bytes:
        raise UploadSessionError(
            "this upload has already received all the bytes it declared"
        )

    destination = _chunk_path(session, index)
    # Written beside and renamed: a request cut off mid-write leaves a .tmp
    # behind rather than a short chunk that looks complete.
    staging = destination.with_suffix(".tmp")
    staging.write_bytes(data)
    os.replace(staging, destination)


def assemble(session: UploadSession, destination: str) -> int:
    """Concatenate the chunks into ``destination``. Returns bytes written.

    Refuses an incomplete or wrong-sized result rather than handing a truncated
    archive to validation, where it would surface as a confusing complaint
    about the zip rather than about the transfer.
    """
    missing = sorted(set(range(session.total_chunks)) - set(received_chunks(session)))
    if missing:
        shown = ", ".join(str(index) for index in missing[:5])
        raise UploadSessionError(
            f"{len(missing)} chunk(s) never arrived (first missing: {shown})"
        )

    written = 0
    with open(destination, "wb") as sink:
        for index in range(session.total_chunks):
            with open(_chunk_path(session, index), "rb") as source:
                while block := source.read(_ASSEMBLE_CHUNK_BYTES):
                    written += len(block)
                    sink.write(block)

    if written != session.total_bytes:
        raise UploadSessionError(
            f"the assembled upload is {written} bytes but {session.total_bytes} "
            "were declared; the transfer did not arrive intact"
        )
    return written


def discard_session(session: UploadSession) -> None:
    """Remove a session and everything it holds. Safe to call twice."""
    shutil.rmtree(session.directory, ignore_errors=True)


def sweep_expired(root: Path, *, ttl_seconds: float) -> int:
    """Delete sessions older than ``ttl_seconds``. Returns how many went.

    Called when a session is opened rather than on a timer: an abandoned upload
    costs disk until something notices, and the moment someone asks for more
    disk is exactly when it is worth reclaiming. No scheduler to configure, and
    no background task to keep alive.
    """
    if not root.exists():
        return 0
    cutoff = time.time() - ttl_seconds
    removed = 0
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        try:
            upload_id = uuid.UUID(entry.name)
        except ValueError:
            continue
        session = load_session(root, upload_id)
        # A directory with no manifest is swept on age alone; it is either a
        # create that died between mkdir and write, or not ours at all.
        age_source = session.created_at if session else entry.stat().st_mtime
        if age_source < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed
