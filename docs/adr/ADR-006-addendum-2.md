# ADR-006 addendum 2: getting the trained model out

## Status
Accepted. Extends ADR-006, which defined how checkpoints are *written*. Nothing
about the write path changes.

## Context

ADR-006 gave the system durable checkpoints and resume-on-reassignment, and both
work. What it never gave anyone was a way to *retrieve* the result.

The state before this change: a job trains to 99% accuracy, the dashboard shows
the number, and the model itself sits in MinIO reachable only by opening the
storage console with separate credentials and guessing which object belongs to
which job. `checkpoint_key` appeared in **zero** response schemas.

For a system whose entire purpose is producing trained models, the output had no
front door. That made it a demonstration of distributed training rather than a
tool someone could use to get a model.

## Decision

### 1. The manifest is the source of truth, not the database

`GET /jobs/{id}/checkpoint` reads the job's manifest from object storage and
reports the `latest` entry.

The database is not a usable alternative here, and the reason is easy to miss.
Postgres records a `checkpoint_key` only in the narrow case where a job
*resumed* from one — that is what the M6 resume event carries. A job that trained
straight through and saved ten checkpoints has none of them in the database.
Serving downloads from the database would silently work for resumed jobs and
fail for everyone else, which is a worse failure than none at all.

The manifest is also the safer source: ADR-006 has the trainer write the blob
first and the manifest second, so a manifest entry cannot name a half-written
file.

Parsing reuses `trainer/checkpoint.py`'s own reader rather than a second
implementation, so the two cannot drift about what a manifest means. That module
is deliberately torch-free at import time, so the control plane does not acquire
a PyTorch dependency by reading it.

### 2. The orchestrator streams the bytes; it does not hand out a presigned URL

This reverses the approach used for datasets (ADR-014 §4), and the reason is
specific rather than a change of taste.

A presigned URL is signed for **one specific host**. The orchestrator signs
against its own `S3_ENDPOINT_URL`, which in the compose topology is
`http://minio:9000` — correct for a peer container on that network, and
unresolvable from a browser on the host. The signature covers the host, so the
URL cannot be rewritten client-side to `localhost:9010` without invalidating it.

The alternative was a second setting — a public-facing endpoint URL used only for
browser-bound links. Rejected: it is a configuration trap. Set it wrong and the
feature appears to work right up until the click, then fails with an opaque DNS
or signature error, and nothing in the health check would notice.

Streaming has none of that. It works from anywhere the API is reachable,
including through the dashboard's dev proxy and over the ADR-010 Tailscale
overlay, with no new configuration at all.

The cost — the control plane carrying the bytes — is the thing ADR-014 avoided
for datasets, and the difference is the access pattern. A dataset is pulled by
every peer on every claim, repeatedly, and can be gigabytes. A model is pulled by
one person, occasionally, when they click a button. Blocking boto3 reads are
iterated in a threadpool by Starlette, and the body is chunked, so a large model
neither blocks the event loop nor is held in memory.

### 3. The caller picks a job, never an object key

The download route takes only `job_id` and resolves the key from the manifest
server-side. Accepting a key would turn this into a way to read any object in the
checkpoints bucket, including other people's models. There is a test asserting a
crafted `?key=` query parameter is ignored.

### 4. Any authenticated user, matching the rest of the jobs router

Not admin-gated. Someone who can already read a job's loss curve, logs, and final
accuracy is not meaningfully restrained by being denied its weights, and adding a
second privilege level to one route in an otherwise uniform router would be
inconsistent for no security gain.

### 5. "No checkpoint" and "storage is down" are different answers

A job with no saved model returns 404 with a message explaining that
checkpointing needs object storage configured on the peer that ran it. Storage
being unreachable returns **503**.

Conflating them would tell someone their model does not exist when the truth is
that MinIO is down, sending them to look for a training bug that is not there.
This is the same distinction the Google sign-in path makes between a refused
credential and an unreachable Google.

### 6. Sizes and losses are reported or null, never guessed

`size_bytes` is null when object storage will not report it, and `loss` is null
when the manifest entry carries none. Neither is defaulted to 0 or interpolated
(CONTRIBUTING.md rule 2). A downloaded file whose size shows as "unknown" is
honest; one that shows "0 B" is wrong.

## Consequences

- No schema change and no migration. Everything read here already existed in
  object storage.
- `orchestrator/services/object_store.py` grows a shared `_BucketStore` base with
  `DatasetObjectStore` and `CheckpointObjectStore` on top, plus `get_bytes`,
  `head_size_bytes`, and a chunked `iter_object`. The dataset path is unchanged.
- The dashboard's job page gains a **Trained model** card that renders only when
  a checkpoint exists, with the size, the epoch/step it was saved at, and a
  snippet showing how to load it.
- The download is fetched as a blob rather than a plain link, because the API
  requires a bearer token and a plain navigation would not send one.
- **Stated plainly in the UI: a `.pt` file is a Python pickle, so loading one
  executes code.** The card says so and tells the reader to load only
  checkpoints they produced. That is the same hazard ADR-014 refused to accept in
  the *upload* direction; here the file is the user's own output and the warning
  is the appropriate control rather than a refusal.
- **Accepted limitation: only the latest checkpoint is retrievable.** The
  manifest's `latest` is what gets served; earlier blobs remain in storage with
  no listing endpoint. A "download the checkpoint from epoch 3" feature would
  need a history view, and nobody has asked for one.
- **Accepted limitation: no integrity check on the way out.** Datasets carry a
  SHA-256 the peer verifies, because that archive crosses a trust boundary. A
  model leaving the system does not, so nothing is verified here beyond what
  object storage itself guarantees.
