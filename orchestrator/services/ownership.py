"""Who may see what: a job's and a dataset's contents belong to their owner.

Before this, any signed-in user could list every job, read its logs (which name
the dataset's classes), chart its metrics, download its trained model, and list
everyone's uploaded datasets -- including the person hosting the orchestrator,
who has no business with another user's data or model. And only admins could
upload a dataset, so every person who wanted to train on their own data had to
be made an admin, which made the first problem worse.

The rules, in one place so every route applies the same ones:

* **Contents are the owner's alone.** A job's logs, metrics and trained model,
  and a dataset's details, are readable only by whoever submitted or uploaded
  it. Admins included: they run the fleet, not other people's work.
* **Admins see that work exists, not what it is.** An admin sees every job's
  state and placement -- managing machines needs that -- and can cancel a job
  or delete a dataset to clean up, without being able to read either.
* **Everyone else sees nothing of it.** A job or dataset another operator owns
  answers 404, not 403, so its existence is not confirmed either.

Ownership is the username recorded at creation (``Job.submitted_by``,
``Dataset.created_by``), taken from the authenticated token, never the request.
"""

from __future__ import annotations

import uuid
from typing import TypeVar

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.models.dataset import Dataset
from orchestrator.models.job import Job
from orchestrator.models.user import User, UserRole
from orchestrator.schemas.job import JobSummary

_NOT_FOUND_JOB = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown job")
_NOT_FOUND_DATASET = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="unknown dataset"
)
_OWNER_ONLY = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="only the person who submitted this job can see its logs, metrics and model",
)


def is_admin(user: User) -> bool:
    return user.role is UserRole.ADMIN


def owns_job(user: User, job: Job) -> bool:
    return job.submitted_by == user.username


def owns_dataset(user: User, dataset: Dataset) -> bool:
    return dataset.created_by == user.username


async def job_visible_to(session: AsyncSession, job_id: uuid.UUID, user: User) -> Job:
    """The job if ``user`` may know it exists (its owner, or an admin), else 404."""
    job = await session.get(Job, job_id)
    if job is None or not (owns_job(user, job) or is_admin(user)):
        raise _NOT_FOUND_JOB
    return job


async def job_contents_for(session: AsyncSession, job_id: uuid.UUID, user: User) -> Job:
    """The job if ``user`` may read its contents (its owner only).

    404 to a user who may not even see the job; 403 to an admin, who can see
    the job exists, so saying why is no leak.
    """
    job = await job_visible_to(session, job_id, user)
    if not owns_job(user, job):
        raise _OWNER_ONLY
    return job


def dataset_owned_by(dataset: Dataset | None, user: User) -> Dataset:
    """``dataset`` if ``user`` owns it, else 404 (including for admins)."""
    if dataset is None or not owns_dataset(user, dataset):
        raise _NOT_FOUND_DATASET
    return dataset


_Summary = TypeVar("_Summary", bound=JobSummary)

#: What a job's uploaded dataset is called, to anyone but its submitter.
PRIVATE_DATASET_LABEL = "private dataset"


def redacted_for(summary: _Summary, user: User) -> _Summary:
    """``summary`` as ``user`` may see it.

    The submitter sees everything. Anyone else who may see the job at all (an
    admin) sees its state and placement but not its result -- the final
    accuracy and loss are metrics of someone else's work -- nor what its
    uploaded dataset is called, since a name like "patient-scans" says more
    than it should.
    """
    if summary.submitted_by == user.username:
        return summary
    update: dict[str, object] = {"result": None}
    if summary.spec.get("dataset_id"):
        update["dataset_name"] = PRIVATE_DATASET_LABEL
    return summary.model_copy(update=update)
