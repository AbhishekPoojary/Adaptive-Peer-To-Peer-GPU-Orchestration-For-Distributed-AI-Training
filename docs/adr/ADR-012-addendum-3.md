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

## Revision: lending your own computer

Adding a machine was admin-only, so a friend who wanted to lend a computer had
to wait for an admin to mint a join command and send it over. Now any signed-in
user may mint one for **their own** computer (`require_enroller`), while
`ALLOW_SELF_LENDING` is on (the default):

- The token's `created_by` is the caller's username, never the request body's
  label (that label is kept only for the static admin key, which has no user).
- `register_node` copies it to `nodes.enrolled_by` (migration
  `0016_node_enrolled_by`; NULL for machines enrolled earlier).
- `DELETE /nodes/{id}` is allowed to an admin or to the node's `enrolled_by`
  user; anyone else gets 403. The idle-only rule is unchanged.
- Minting is limited per account, so a signed-in user cannot mint in bulk.
  Listing and revoking tokens stay admin-only.

The trade-off is stated where it is configured: a lent computer receives other
users' training data while it trains their jobs, so with sign-ups open, anyone
holding the link can lend a computer and see the data of jobs placed on it.
That was already true of anyone an admin enrolled; what changes is who decides.
`ALLOW_SELF_LENDING=false` returns that decision to admins.
