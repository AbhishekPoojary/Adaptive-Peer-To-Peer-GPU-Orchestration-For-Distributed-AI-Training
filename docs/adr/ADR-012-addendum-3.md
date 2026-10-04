# ADR-012 addendum 3: a user's data and model are theirs alone

## Status
Accepted (amends ADR-012 §2 and ADR-014)

## Context
ADR-012 §2 defined two roles: OPERATOR submits, cancels and reads; ADMIN also
administers the fleet. ADR-014 made uploading a dataset ADMIN-only, on the
grounds that a dataset runs on other people's machines. In use, both turned out
wrong for the people the system is for:

* **Everyone who wanted to train on their own data had to be made an admin.**
  Uploading was the only way to use your own images, and only admins could do
  it, so admin became the role for "a person who trains".
* **Nothing was private.** Any signed-in user could list every job, read its
  logs (which name the dataset's classes), chart its metrics, download its
  trained model, and list everyone's datasets. That included the person hosting
  the orchestrator, who has no business with another user's data or model.

## Decision
Ownership is recorded at creation from the authenticated token
(`Job.submitted_by`, `Dataset.created_by`) and decided in one place,
`orchestrator/services/ownership.py`:

| | Owner | Admin (not owner) | Another operator |
|---|---|---|---|
| Upload a dataset | yes (anyone signed in) | yes | yes |
| List or read a dataset | yes | no (404) | no (404) |
| Train on a dataset | yes | no | no |
| Delete a dataset | yes | yes, to clean up | no (404) |
| See a job exists, its state and placement | yes | yes | no (404) |
| A job's result, logs, metrics, trained model | yes | no (403) | no (404) |
| A job's uploaded-dataset name | yes | shown as "private dataset" | no |
| Cancel a job | yes | yes | no (404) |

Admins keep what running the fleet needs (what is running where, and the power
to stop or remove it) and lose what it does not (reading anyone's work). A user
who may not see a job or dataset at all gets 404, so its existence is not
confirmed; an admin who may see a job but not its contents gets 403, which
says why without leaking anything they could not already see.

Dataset names become unique per uploader (migration 0015). A global rule would
let one user learn another's dataset names from a "name taken" error, and would
make two people who both call an upload "cats" collide for no reason.

## Consequences
* A person who trains needs only an ordinary account.
* The job detail carries `contents_visible`, so the dashboard shows a short
  "private to <user>" note instead of polling for logs it may not read.
* **What this does not cover: the machine that trains the job.** A peer that
  runs your job necessarily holds your dataset (in its cache volume, which
  persists so later jobs skip the download) and your model (in memory while
  training). Application permissions cannot stop that machine's owner reading
  them. Training only on peers you trust remains the real control; removing
  the dataset cache after each job would narrow the window at the cost of
  re-downloading on every run.
* The orchestrator's operator still holds MinIO's credentials and could read
  stored objects directly. This decision governs the application, not someone
  with root on the server.
