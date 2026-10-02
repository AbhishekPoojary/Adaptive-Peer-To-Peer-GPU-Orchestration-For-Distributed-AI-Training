"""Checkpoint reads and writes on a trainer's behalf (ADR-006 addendum 3).

Before this, a trainer checkpointed only if its peer had been handed the MinIO
credentials, which no installed peer ever was. Those credentials open every
object in the bucket, so handing them out is not an option either: a peer could
read every other job's model, or overwrite it. The result was that a job whose
peer died restarted from step zero on the next one, although the checkpoint and
resume machinery existed and worked.

So the orchestrator holds the credentials and the trainer asks it, using a
token minted for one lease at claim time. Every request is decided here against
the live database rather than against the token alone:

* the lease named by the token must be the job's **current-epoch, ACTIVE**
  lease. This is the same fence the lease endpoints apply, so a trainer that has
  been declared dead and superseded cannot write over its successor's
  checkpoint even though its token has not expired;
* only **rank 0** may write, matching ADR-006's single-writer rule. Every rank
  may read, since every rank restores on resume;
* the key must belong to **this job**: its manifest, or a blob under its own
  prefix. A token for one job can never name another job's objects.

What it does not close: a check-then-write window of one request. A lease
fenced between the check and the MinIO write lands that one write. It is
harmless for the reason ADR-006 already relies on: blob keys are unique per
write, and the superseded trainer is killed by its agent within one renewal
cycle of losing the lease.
"""

from __future__ import annotations

import posixpath
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.models.job import Job
from orchestrator.models.lease import Lease, LeaseState


class CheckpointAccessDeniedError(Exception):
    """The request is not allowed. ``status`` is the HTTP code to answer with."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class CheckpointGrant:
    """A request that passed every check: the lease and job it acts for."""

    lease: Lease
    job: Job


def key_belongs_to_job(key: str, job_id: str) -> bool:
    """True iff ``key`` is this job's manifest or one of its checkpoint blobs.

    The comparison is on the normalised path, so ``checkpoints/<job>/../<other>``
    cannot climb out of the job's prefix, and anything that normalises
    differently from how it was written is refused outright rather than being
    reinterpreted.
    """
    if not key or key.startswith("/") or "\\" in key:
        return False
    if posixpath.normpath(key) != key:
        return False
    if key == f"manifests/{job_id}.json":
        return True
    prefix = f"checkpoints/{job_id}/"
    return key.startswith(prefix) and "/" not in key[len(prefix) :]


async def authorize(
    session: AsyncSession,
    *,
    token_lease_id: str,
    token_job_id: str,
    path_lease_id: uuid.UUID,
    key: str,
    write: bool,
) -> CheckpointGrant:
    """Decide one checkpoint request. Raises :class:`CheckpointAccessDeniedError`.

    The order gives the most useful answer first: a token for a different
    lease is 403 before anything is looked up, a missing lease is 404, a
    superseded or finished one is 409 (the same code the lease endpoints use
    for a fenced-out holder, which the trainer treats as "stop, you have been
    replaced"), and a key outside the job is 403.
    """
    if token_lease_id != str(path_lease_id):
        raise CheckpointAccessDeniedError(403, "token was issued for a different lease")

    lease = await session.get(Lease, path_lease_id)
    if lease is None:
        raise CheckpointAccessDeniedError(404, "unknown lease")
    if str(lease.job_id) != token_job_id:
        raise CheckpointAccessDeniedError(403, "token was issued for a different job")

    job = await session.get(Job, lease.job_id)
    if job is None:  # pragma: no cover - FK guarantees it
        raise CheckpointAccessDeniedError(404, "unknown job")

    if lease.lease_epoch != job.current_lease_epoch:
        raise CheckpointAccessDeniedError(
            409, "stale lease epoch; this attempt has been superseded"
        )
    if lease.state is not LeaseState.ACTIVE:
        raise CheckpointAccessDeniedError(409, "lease is not active")
    if write and lease.rank != 0:
        raise CheckpointAccessDeniedError(
            403, "only rank 0 writes checkpoints (ADR-006 single writer)"
        )
    if not key_belongs_to_job(key, str(job.id)):
        raise CheckpointAccessDeniedError(403, "key is outside this job's checkpoints")
    return CheckpointGrant(lease=lease, job=job)
