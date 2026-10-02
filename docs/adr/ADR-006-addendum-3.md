# ADR-006 addendum 3: checkpoints go through the orchestrator

## Status
Accepted (amends ADR-006 and ADR-006 addendum)

## Context
ADR-006 has the trainer write checkpoints straight to MinIO, which needs the
bucket's credentials inside the trainer. The agent passed them through when it
was started with `S3_ENDPOINT_URL`/`S3_ACCESS_KEY`/`S3_SECRET_KEY`. Nothing
ever started it that way:

* the installer never configured them, and should not have, because they are
  MinIO's root keys. With them a peer can read every job's model and every
  uploaded dataset, and overwrite or delete any of it;
* the benchmark harness did not pass them either.

So on every peer that actually ran jobs, checkpointing was silently off. A job
whose peer died was reassigned correctly, then retrained from step zero. Both
`failure_recovery` runs show it: no "resumed from checkpoint" event, and the
replacement trained all four epochs (`20260728T161648`, `20261002T161140`). The
resume path was implemented and unit-tested, but nothing deployed ever used it.

## Decision
The orchestrator already holds the bucket credentials, so it stores and serves
checkpoint objects on the trainer's behalf:

```
GET /leases/{lease_id}/checkpoint-objects/{key}
PUT /leases/{lease_id}/checkpoint-objects/{key}
Authorization: Bearer <checkpoint token>
```

**The credential.** At claim time the orchestrator mints a JWT with audience
`checkpoint`, subject the lease id, and the job id. It travels in the claim
response as `checkpoint_token`, beside the spec rather than inside it, because
the spec is logged and stored and this is a secret. The agent puts it in the
trainer's environment with the orchestrator's URL. Loopback is rewritten to
`host.docker.internal` for a container, which cannot reach the host as
`localhost`.

**The rules**, applied per request against the database
(`services/checkpoint_access.py`):

1. The lease must be the job's **current-epoch, ACTIVE** lease. This is the
   same fence the lease endpoints apply, so a trainer whose peer was declared
   dead cannot write over its successor's checkpoint, whatever its token's
   expiry says. The token's own TTL (24 h) is only an outer bound.
2. Only **rank 0** writes (ADR-006's single writer); every rank reads.
3. The key must be this job's manifest or a blob directly under its prefix,
   compared after normalisation, so a token never names another job's objects.
4. Bodies are capped (`CHECKPOINT_MAX_BYTES`, 512 MiB), because the
   orchestrator buffers them before forwarding.

The trainer gets `ApiObjectStore`, which implements the same two-method
`ObjectStore` Protocol as `S3ObjectStore`, over urllib. The manifest logic
(blob first, then manifest) does not change at all; it never knew what store it
was talking to. A 404 means "not found" (the first attempt has no manifest),
while a 409 or 503 is an error, so neither a superseded attempt nor an outage
is mistaken for "nothing to resume from".

The direct-S3 path stays, behind the token: a co-located setup that configures
`S3_*` explicitly still works, but a token always wins.

## Consequences
* **Checkpoint/resume works on installed peers**, with no new peer setup and no
  storage credentials on any peer.
* **Checkpoint bytes cross the orchestrator twice** (peer → orchestrator →
  MinIO). For SmallCNN's few-MB checkpoints every 100 steps this is negligible;
  for a large model on a slow uplink it would not be, and it is the first thing
  to measure before supporting one.
* **One request's check-then-write window.** A lease fenced between the check
  and the MinIO write lands that one write. It is harmless for the reason ADR-006
  already relies on: blob keys are unique per write, and the superseded
  trainer's agent stops it within a renewal cycle of losing the lease.
* The trainer image must be rebuilt to pick up `ApiObjectStore`. An old image
  ignores the new variables and runs without checkpoints, exactly as before.
